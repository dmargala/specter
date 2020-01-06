#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import absolute_import, division, print_function, unicode_literals

import sys
import numpy as np
import scipy.sparse
import scipy.linalg
from scipy.sparse import spdiags, issparse
from scipy.sparse.linalg import spsolve

from specter.util import outer

import cupy as cp
import cupyx as cpx

def ex2d(image, imageivar, psf, specmin, nspec, wavelengths, xyrange=None,
         regularize=0.0, ndecorr=False, bundlesize=25, nsubbundles=1,
         wavesize=50, full_output=False, verbose=False, 
         debug=False, psferr=None):
    '''
    2D PSF extraction of flux from image patch given pixel inverse variance.
    
    Inputs:
        image : 2D array of pixels
        imageivar : 2D array of inverse variance for the image
        psf   : PSF object
        specmin : index of first spectrum to extract
        nspec : number of spectra to extract
        wavelengths : 1D array of wavelengths to extract
    Optional Inputs:
        xyrange = (xmin, xmax, ymin, ymax): treat image as a subimage
            cutout of this region from the full image
        regularize: experimental regularization factor to minimize ringing
        ndecorr : if True, decorrelate the noise between fibers, at the
            cost of residual signal correlations between fibers.
        bundlesize: extract in groups of fibers of this size, assuming no
            correlation with fibers outside of this bundle
        nsubbundles: (int) number of overlapping subbundles to use per bundle
        wavesize: number of wavelength steps to include per sub-extraction
        full_output: Include additional outputs based upon chi2 of model
            projected into pixels
        verbose: print more stuff
        debug: if True, enter interactive ipython session before returning
        psferr:  fractional error on the psf model. if not None, use this
            fractional error on the psf model instead of the value saved
            in the psf fits file. This is used only to compute the chi2,
            not to weight pixels in fit
    Returns (flux, ivar, Rdata):
        flux[nspec, nwave] = extracted resolution convolved flux
        ivar[nspec, nwave] = inverse variance of flux
        Rdata[nspec, 2*ndiag+1, nwave] = sparse Resolution matrix data
    TODO: document output if full_output=True
    ex2d uses divide-and-conquer to extract many overlapping subregions
    and then stitches them back together.  Params wavesize and bundlesize
    control the size of the subregions that are extracted; the necessary
    amount of overlap is auto-calculated based on PSF extent.
    '''
    #- TODO: check input dimensionality etc.

    dw = wavelengths[1] - wavelengths[0]
    if not np.allclose(dw, np.diff(wavelengths)):
        raise ValueError('ex2d currently only supports linear wavelength grids')

    #add this since we don't have subbundles
    speclo = specmin
    spechi = specmin + nspec
    specrange = (speclo, spechi)

    #keep
    #[ True  True  True  True False]

    keep = np.ones(25,dtype=bool) #keep the whole bundle, is this right?

    #- Output arrays to fill
    nwave = len(wavelengths)
    flux = np.zeros( (nspec, nwave) )
    ivar = np.zeros( (nspec, nwave) )
    if full_output:
        pixmask_fraction = np.zeros( (nspec, nwave) )
        chi2pix = np.zeros( (nspec, nwave) )
        modelimage = np.zeros_like(image)

    #- Diagonal elements of resolution matrix
    #- Keep resolution matrix terms equivalent to 9-sigma of largest spot
    #- ndiag is in units of number of wavelength steps of size dw
    ndiag = 0
    for ispec in [specmin, specmin+nspec//2, specmin+nspec-1]:
        for w in [psf.wmin, 0.5*(psf.wmin+psf.wmax), psf.wmax]:
            ndiag = max(ndiag, int(round(9.0*psf.wdisp(ispec, w) / dw )))

    #- make sure that ndiag isn't too large for actual PSF spot size
    wmid = (psf.wmin_all + psf.wmax_all) / 2.0
    spotsize = psf.pix(0, wmid).shape
    ndiag = min(ndiag, spotsize[0]//2, spotsize[1]//2)

    #- Orig was ndiag = 10, which fails when dw gets too large compared to PSF size
    Rd = np.zeros( (nspec, 2*ndiag+1, nwave) )

    if psferr is None :
        psferr = psf.psferr

    #- Let's do some extractions
    #but no subbundles! wavelength patches only
    for iwave in range(0, len(wavelengths), wavesize):
        #- Low and High wavelengths for the core region
        wlo = wavelengths[iwave]
        if iwave+wavesize < len(wavelengths):
            whi = wavelengths[iwave+wavesize]
        else:
            whi = wavelengths[-1]
        
        #- Identify subimage that covers the core wavelengths
        subxyrange = xlo,xhi,ylo,yhi = psf.xyrange(specrange, (wlo, whi))
        
        if xyrange is None:
            subxy = np.s_[ylo:yhi, xlo:xhi]
        else:
            subxy = np.s_[ylo-xyrange[2]:yhi-xyrange[2], xlo-xyrange[0]:xhi-xyrange[0]]
        
        subimg = image[subxy]
        subivar = imageivar[subxy]
        
        #- Determine extra border wavelength extent: nlo,nhi extra wavelength bins
        ny, nx = psf.pix(speclo, wlo).shape
        ymin = ylo-ny+2
        ymax = yhi+ny-2
        
        nlo = max(int((wlo - psf.wavelength(speclo, ymin))/dw)-1, ndiag)
        nhi = max(int((psf.wavelength(speclo, ymax) - whi)/dw)-1, ndiag)
        ww = np.arange(wlo-nlo*dw, whi+(nhi+0.5)*dw, dw)
        wmin, wmax = ww[0], ww[-1]
        nw = len(ww)

        #- include \r carriage return to prevent scrolling
        if verbose:
            sys.stdout.write("\rSpectra {specrange} wavelengths ({wmin:.2f}, {wmax:.2f}) -> ({wlo:.2f}, {whi:.2f})".format(\
                specrange=specrange, wmin=wmin, wmax=wmax, wlo=wlo, whi=whi))
            sys.stdout.flush()
        
        #- Do the extraction with legval cache as default
        results = ex2d_patch(subimg, subivar, psf,
        specmin=speclo, nspec=spechi-speclo, wavelengths=ww,
        xyrange=[xlo,xhi,ylo,yhi])
        
        specflux = results['flux']
        specivar = results['ivar']
        R = results['R']
       
        #- Fill in the final output arrays
        ## iispec = slice(speclo-specmin, spechi-specmin)

        #since we don't have subbundles maybe we can get rid of this?
        iispec = np.arange(speclo-specmin, spechi-specmin)
        flux[iispec[keep], iwave:iwave+wavesize+1] = specflux[keep, nlo:-nhi]
        ivar[iispec[keep], iwave:iwave+wavesize+1] = specivar[keep, nlo:-nhi]

        if full_output:
            A = results['A'].copy()
            xflux = results['xflux']
            
            #- number of spectra and wavelengths for this sub-extraction
            subnspec = spechi-speclo
            subnwave = len(ww)
            
            #- Model image
            submodel = A.dot(xflux.ravel()).reshape(subimg.shape)
            
            #- Fraction of input pixels that are unmasked for each flux bin
            subpixmask_fraction = 1.0-(A.T.dot(subivar.ravel()>0)).reshape(subnspec, subnwave)
            
            #- original weighted chi2 of pixels that contribute to each flux bin
            # chi = (subimg - submodel) * np.sqrt(subivar)
            # chi2x = (A.T.dot(chi.ravel()**2) / A.sum(axis=0)).reshape(subnspec, subnwave)
            
            #- pixel variance including input noise and PSF model errors
            modelivar = (submodel*psferr + 1e-32)**-2
            ii = (modelivar > 0) & (subivar > 0)
            totpix_ivar = np.zeros(submodel.shape)
            totpix_ivar[ii] = 1.0 / (1.0/modelivar[ii] + 1.0/subivar[ii])
            
            #- Weighted chi2 of pixels that contribute to each flux bin;
            #- only use unmasked pixels and avoid dividing by 0
            chi = (subimg - submodel) * np.sqrt(totpix_ivar)
            psfweight = A.T.dot(totpix_ivar.ravel()>0)
            bad = (psfweight == 0.0)
            chi2x = (A.T.dot(chi.ravel()**2) * ~bad) / (psfweight + bad)
            chi2x = chi2x.reshape(subnspec, subnwave)
            
            #- outputs
            #- TODO: watch out for edge effects on overlapping regions of submodels
            modelimage[subxy] = submodel
            pixmask_fraction[iispec[keep], iwave:iwave+wavesize+1] = subpixmask_fraction[keep, nlo:-nhi]
            chi2pix[iispec[keep], iwave:iwave+wavesize+1] = chi2x[keep, nlo:-nhi]

        #- Fill diagonals of resolution matrix
        for ispec in np.arange(speclo, spechi)[keep]:
            #- subregion of R for this spectrum
            ii = slice(nw*(ispec-speclo), nw*(ispec-speclo+1))
            Rx = R[ii, ii]

            for j in range(nlo,nw-nhi):
                # Rd dimensions [nspec, 2*ndiag+1, nwave]
                Rd[ispec-specmin, :, iwave+j-nlo] = Rx[j-ndiag:j+ndiag+1, j]

    #- Add extra print because of carriage return \r progress trickery
    if verbose:
        print()

    #+ TODO: what should this do to R in the case of non-uniform bins?
    #+       maybe should do everything in photons/A from the start.            
    #- Convert flux to photons/A instead of photons/bin
    dwave = np.gradient(wavelengths)
    flux /= dwave
    ivar *= dwave**2

    if debug:
        #--- DEBUG ---
        import IPython
        IPython.embed()
        #--- DEBUG ---
    
    if full_output:
        return dict(flux=flux, ivar=ivar, resolution_data=Rd, modelimage=modelimage,
            pixmask_fraction=pixmask_fraction, chi2pix=chi2pix)
    else:
        return flux, ivar, Rd

#since we renamed this unit tests are going to fail
def ex2d_patch(image, ivar, psf, specmin, nspec, wavelengths, xyrange, regularize=0.0, 
        ndecorr=False, use_cache=None):
    """
    2D PSF extraction of flux from image patch given pixel inverse variance.
    
    Inputs:
        image : 2D array of pixels
        ivar  : 2D array of inverse variance for the image
        psf   : PSF object
        specmin : index of first spectrum to extract
        nspec : number of spectra to extract
        wavelengths : 1D array of wavelengths to extract
        
    Optional Inputs:
        xyrange = (xmin, xmax, ymin, ymax): treat image as a subimage
            cutout of this region from the full image
        full_output : if True, return a dictionary of outputs including
            intermediate outputs such as the projection matrix.
        ndecorr : if True, decorrelate the noise between fibers, at the
            cost of residual signal correlations between fibers.
        use_cache: default behavior, can be turned off for testing purposes
    Returns (flux, ivar, R):
        flux[nspec, nwave] = extracted resolution convolved flux
        ivar[nspec, nwave] = inverse variance of flux
        R : 2D resolution matrix to convert
    """
   
    specrange = (specmin, specmin+nspec)
    ny, nx = image.shape
    npix = ny*nx
    nwave = len(wavelengths)

    #get noisyimg
    noisyimg_gpu = cp.asarray(image)
    
    #get imgweights 
    imgweights_gpu = cp.asarray(ivar)
    
    #get A(eventually replace with gpuized version)
    A = psf.projection_matrix(specrange, wavelengths, xyrange, use_cache=use_cache)
    A_gpu = cpx.scipy.sparse.csr_matrix(A)
    
    #- Set up the equation to solve (B&S eq 4)
    W_gpu = cpx.scipy.sparse.spdiags(data=imgweights_gpu.ravel(), diags=[0,], m=npix, n=npix)
    
    iCov_gpu = A_gpu.T.dot(W_gpu.dot(A_gpu))
    
    y_gpu = A_gpu.T.dot(W_gpu.dot(noisyimg_gpu.ravel()))   
    
    #- Solve f (B&S eq 4)
    #is this xflux (in ex2d_patch original)
    #f_cpu_old = scipy.sparse.linalg.spsolve(iCov_cpu, y_cpu).reshape(nspec, nwave)
    #print("f_cpu_old.shape")
    #print(f_cpu_old.shape)
    #doesn't exist in cupy yet so let's get creative
    #try cupy.linalg.solve
    #numpy.linalg.LinAlgError: cupy.linalg only supports cupy.core.ndarray
    
    f_gpu_tup = cpx.scipy.sparse.linalg.lsqr(iCov_gpu, y_gpu)
    ##returns f_gpu as a tuple... need to reshape?
    f_gpu_0 = f_gpu_tup[0] #take only zeroth element of tuple, rest are None for some reason
    f_gpu = cp.asarray(f_gpu_0).reshape(nspec, nwave) #the tuple thing is dumb bc i think it goes back to the cpu, have to manually bring it back as a cupy array
    #print("f_cpu.shape")
    #print(f_cpu.shape)

    #- Eigen-decompose iCov to assist in upcoming steps
    #at lesat we know this one work
    u_gpu, v_gpu = cp.linalg.eigh(iCov_gpu.todense())
    #we can skip the checking here
    
    #- Calculate C^-1 = QQ (B&S eq 10)
    d_gpu = cpx.scipy.sparse.spdiags(cp.sqrt(u_gpu), 0, len(u_gpu) , len(u_gpu))
    
    Q_gpu = v_gpu.dot( d_gpu.dot( v_gpu.T ))
    
    #- normalization vector (B&S eq 11)
    norm_vector_gpu = cp.sum(Q_gpu, axis=1)
    
    #- Resolution matrix (B&S eq 12)
    R_gpu = cp.outer(norm_vector_gpu**(-1), cp.ones(norm_vector_gpu.size)) * Q_gpu
    
    #- Decorrelated covariance matrix (B&S eq 13-15)
    udiags_gpu = cpx.scipy.sparse.spdiags(1/u_gpu, 0, len(u_gpu), len(u_gpu))
    
    Cov_gpu = v_gpu.dot( udiags_gpu.dot (v_gpu.T ))
    
    Cx_gpu = R_gpu.dot(Cov_gpu.dot(R_gpu.T))
    
    #- Decorrelated flux (B&S eq 16)
    fx_gpu = R_gpu.dot(f_gpu.ravel()).reshape(f_gpu.shape)
    
    #- Variance on f (B&S eq 13)
    varfx_gpu = cp.diagonal(Cx_gpu).reshape(f_gpu.shape)

    #gonna need to return the full output
    #are these guesses right?

    #for now let's bring all the gpu values back to the host before we return
    fx_cpu = fx_gpu.get()
    varfx_cpu = varfx_gpu.get()
    R_cpu = R_gpu.get()
    f_cpu = f_gpu.get()
    A_cpu = A_gpu.get()
    iCov_cpu = iCov_gpu.get()

    results = dict(flux=fx_cpu, ivar=varfx_cpu, R=R_cpu, xflux=f_cpu, A=A_cpu, iCov=iCov_cpu)
    results['options'] = dict(
        specmin=specmin, nspec=nspec, wavelengths=wavelengths,
        xyrange=xyrange, regularize=regularize, ndecorr=ndecorr
        )
    return results


def eigen_compose(w, v, invert=False, sqr=False):
    """
    Create a matrix from its eigenvectors and eigenvalues.
    Given the eigendecomposition of a matrix, recompose this
    into a real symmetric matrix.  Optionally take the square
    root of the eigenvalues and / or invert the eigenvalues.
    The eigenvalues are regularized such that the condition 
    number remains within machine precision for 64bit floating 
    point values.
    Args:
        w (array): 1D array of eigenvalues
        v (array): 2D array of eigenvectors.
        invert (bool): Should the eigenvalues be inverted? (False)
        sqr (bool): Should the square root eigenvalues be used? (False)
    Returns:
        A 2D numpy array which is the recomposed matrix.
    """
    dim = w.shape[0]

    # Threshold is 10 times the machine precision (~1e-15)
    threshold = 10.0 * sys.float_info.epsilon

    maxval = np.max(w)
    wscaled = np.zeros_like(w)

    if invert:
        # Normally, one should avoid explicit loops in python.
        # in this case however, we need to conditionally invert
        # the eigenvalues only if they are above the threshold.
        # Otherwise we might divide by zero.  Since the number
        # of eigenvalues is never too large, this should be fine.
        # If it does impact performance, we can improve this in
        # the future.  NOTE: simple timing with an average over
        # 10 loops shows that all four permutations of invert and
        # sqr options take about the same time- so this is not
        # an issue.
        if sqr:
            minval = np.sqrt(maxval) * threshold
            replace = 1.0 / minval
            tempsqr = np.sqrt(w)
            for i in range(dim):
                if tempsqr[i] > minval:
                    wscaled[i] = 1.0 / tempsqr[i]
                else:
                    wscaled[i] = replace
        else:
            minval = maxval * threshold
            replace = 1.0 / minval
            for i in range(dim):
                if w[i] > minval:
                    wscaled[i] = 1.0 / w[i]
                else:
                    wscaled[i] = replace
    else:
        if sqr:
            minval = np.sqrt(maxval) * threshold
            replace = minval
            wscaled[:] = np.where((w > minval), np.sqrt(w), replace*np.ones_like(w))
        else:
            minval = maxval * threshold
            replace = minval
            wscaled[:] = np.where((w > minval), w, replace*np.ones_like(w))

    # multiply to get result
    wdiag = spdiags(wscaled, 0, dim, dim)
    return v.dot( wdiag.dot(v.T) )

def resolution_from_icov(icov, decorr=None):
    """
    Function to generate the 'resolution matrix' in the simplest
    (no unrelated crosstalk) Bolton & Schlegel 2010 sense.
    Works on dense matrices.  May not be suited for production-scale
    determination in a spectro extraction pipeline.
    Args:
        icov (array): real, symmetric, 2D array containing inverse
                      covariance.
        decorr (list): produce a resolution matrix which decorrelates
                      signal between fibers, at the cost of correlated
                      noise between fibers (default).  This list should
                      contain the number of elements in each spectrum,
                      which is used to define the size of the blocks.
    Returns (R, ivar):
        R : resolution matrix
        ivar : R C R.T  -- decorrelated resolution convolved inverse variance
    """
    #- force symmetry since due to rounding it might not be exactly symmetric
    icov = 0.5*(icov + icov.T)
    
    if issparse(icov):
        icov = icov.toarray()

    w, v = scipy.linalg.eigh(icov)

    sqrt_icov = np.zeros_like(icov)

    if decorr is not None:
        if np.sum(decorr) != icov.shape[0]:
            raise RuntimeError("The list of spectral block sizes must sum to the matrix size")
        inverse = eigen_compose(w, v, invert=True)
        # take each spectrum block and process
        offset = 0
        for b in decorr:
            bw, bv = scipy.linalg.eigh(inverse[offset:offset+b,offset:offset+b])
            sqrt_icov[offset:offset+b,offset:offset+b] = eigen_compose(bw, bv, invert=True, sqr=True)
            offset += b
    else:
        sqrt_icov = eigen_compose(w, v, sqr=True)

    norm_vector = np.sum(sqrt_icov, axis=1)

    # R = np.outer(norm_vector**(-1), np.ones(norm_vector.size)) * sqrt_icov
    R = np.empty_like(icov)
    outer(norm_vector**(-1), np.ones(norm_vector.size), out=R)
    R *= sqrt_icov

    ivar = norm_vector**2  #- Bolton & Schlegel 2010 Eqn 13
    return R, ivar

def split_bundle(bundlesize, n):
    '''
    Partitions a bundle into subbundles for extraction
    Args:
        bundlesize: (int) number of fibers in the bundle
        n: (int) number of subbundles to generate
    Returns (subbundles, extract_subbundles) where
    subbundles = list of arrays of indices belonging to each subbundle
    extract_subbundles = list of arrays of indices to extract for each
        subbundle, including edge overlaps except for first and last fiber
    NOTE: resulting partition is such that the lengths of the extract_subbundles
    differ by at most 1.
    
    Example: split_bundle(10, 3) returns
    ([array([0, 1, 2]), array([3, 4, 5]), array([6, 7, 8, 9])],
     [array([0, 1, 2, 3]), array([2, 3, 4, 5, 6]), array([5, 6, 7, 8, 9])])
    '''
    if n > bundlesize:
        raise ValueError('n={} should be less or equal to bundlesize={}'.format(
                         n, bundlesize))

    #- initial partition into subbundles
    n_per_subbundle = [len(x) for x in np.array_split(np.arange(bundlesize), n)]

    #- rearrange to put smaller subbundles in middle instead of at edge,
    #- which can happen when bundlesize % n != 0
    i = 0
    while i < n-1:
        if n_per_subbundle[i] > n_per_subbundle[i+1]:
            n_per_subbundle[i+1], n_per_subbundle[i] = n_per_subbundle[i], n_per_subbundle[i+1]
        i += 1

    #- populate non-overlapping indices for subbundles
    subbundles = list()
    imin = 0
    for nsub in n_per_subbundle:
        subbundles.append(np.arange(imin, imin+nsub, dtype=int))
        imin += nsub

    #- populate overlapping indices for extract_subbundles
    extract_subbundles = list()
    for ii in subbundles:
        ipre  = [ii[0]-1,] if ii[0]>0 else np.empty(0, dtype=int)
        ipost = [ii[-1]+1,] if ii[-1]<bundlesize-1 else np.empty(0, dtype=int)
        extract_subbundles.append( np.concatenate( [ipre, ii, ipost] ) )

    return subbundles, extract_subbundles

#-------------------------------------------------------------------------
#- Utility functions for understanding PSF bias on extractions

def psfbias(p1, p2, wave, phot, ispec=0, readnoise=3.0):
    """
    Return bias from extracting with PSF p2 if the real PSF is p1
    Inputs:
        p1, p2 : PSF objects
        wave[] : wavelengths in Angstroms
        phot[] : spectrum in photons
    Optional Inputs:
        ispec : spectrum number
        readnoise : CCD read out noise (optional)
    Returns:
        bias array same length as wave
    """
    #- flux -> pixels projection matrices
    xyrange = p1.xyrange( (ispec,ispec+1), (wave[0], wave[-1]) )
    A = p1.projection_matrix((ispec,ispec+1), wave, xyrange)
    B = p2.projection_matrix((ispec,ispec+1), wave, xyrange)

    #- Pixel weights from photon shot noise and CCD read noise
    img = A.dot(phot)            #- True noiseless image
    imgvar = readnoise**2 + img  #- pixel variance
    npix = img.size
    W = spdiags(1.0/imgvar, 0, npix, npix)

    #- covariance matrix for each PSF
    iACov = A.T.dot(W.dot(A))
    iBCov = B.T.dot(W.dot(B))
    BCov = np.linalg.inv(iBCov.toarray())

    #- Resolution matricies
    RA, _ = resolution_from_icov(iACov)
    RB, _ = resolution_from_icov(iBCov)

    #- Bias
    bias = (RB.dot(BCov.dot(B.T.dot(W.dot(A)).toarray())) - RA).dot(phot) / RA.dot(phot)

    return bias

def psfabsbias(p1, p2, wave, phot, ispec=0, readnoise=3.0):
    """
    Return absolute bias from extracting with PSF p2 if the real PSF is p1.
    Inputs:
        p1, p2 : PSF objects
        wave[] : wavelengths in Angstroms
        phot[] : spectrum in photons
    Optional Inputs:
        ispec : spectrum number
        readnoise : CCD read out noise (optional)
    Returns bias, R
        bias array same length as wave
        R resolution matrix for PSF p1
    See psfbias() for relative bias
    """
    #- flux -> pixels projection matrices
    xyrange = p1.xyrange( (ispec,ispec+1), (wave[0], wave[-1]) )
    A = p1.projection_matrix((ispec,ispec+1), wave, xyrange)
    B = p2.projection_matrix((ispec,ispec+1), wave, xyrange)

    #- Pixel weights from photon shot noise and CCD read noise
    img = A.dot(phot)            #- True noiseless image
    imgvar = readnoise**2 + img  #- pixel variance
    npix = img.size
    W = spdiags(1.0/imgvar, 0, npix, npix)

    #- covariance matrix for each PSF
    iACov = A.T.dot(W.dot(A))
    iBCov = B.T.dot(W.dot(B))
    BCov = np.linalg.inv(iBCov.toarray())

    #- Resolution matricies
    RA, _ = resolution_from_icov(iACov)
    RB, _ = resolution_from_icov(iBCov)

    #- Bias
    bias = (RB.dot(BCov.dot(B.T.dot(W.dot(A)).toarray())) - RA).dot(phot)

    return bias, RA
