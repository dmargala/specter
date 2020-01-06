#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import absolute_import, division, print_function, unicode_literals

import sys
import numpy as np
import scipy.sparse
import scipy.linalg
from scipy.sparse import spdiags, issparse
from scipy.sparse.linalg import spsolve
import time

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

    #default subbundle setting is 6
    #have to use nsubbundles=1 setting to override!

    #do specrange ourselves (no subbundles)! 
    speclo = specmin
    spechi = specmin + nspec
    specrange = (speclo, spechi)

    #keep
    #[ True  True  True  True False]
    #do keep ourselves too
    keep = np.ones(25,dtype=bool) #keep the whole bundle, is this right? probably not...

    #- TODO: check input dimensionality etc.

    dw = wavelengths[1] - wavelengths[0]
    if not np.allclose(dw, np.diff(wavelengths)):
        raise ValueError('ex2d currently only supports linear wavelength grids')

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
 
        ##this is the same in ex2d and ex2d_patch
        #print("extract subimg.shape")
        #print(subimg.shape)

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

        deltat = []
        tstart = time.time()
        #- Do the extraction with legval cache as default
        results = \
            ex2d_patch(subimg, subivar, psf,
                specmin=speclo, nspec=spechi-speclo, wavelengths=ww,
                xyrange=[xlo,xhi,ylo,yhi], regularize=regularize, ndecorr=ndecorr,
                full_output=True, use_cache=True)       
        tend = time.time()
        deltat.append(tend - tstart)

        #print("time spent ex2d_patch %s" %(deltat))
        

        specflux = results['flux']
        #flux = results['flux']
        specivar = results['ivar']
        #ivar = results['ivar']
        R = results['R']
       
        #- Fill in the final output arrays
        ## iispec = slice(speclo-specmin, spechi-specmin)

        ##since we don't have subbundles maybe we can get rid of this? no!
        #we have to assemble the data from the patches back together!!!
        iispec = np.arange(speclo-specmin, spechi-specmin)

        flux[iispec[keep], iwave:iwave+wavesize+1] = specflux[keep, nlo:-nhi]
        ivar[iispec[keep], iwave:iwave+wavesize+1] = specivar[keep, nlo:-nhi]

        if full_output:
            A = results['A'].copy()
            xflux = results['xflux']
            
            #- number of spectra and wavelengths for this sub-extraction
            subnspec = spechi-speclo
            subnwave = len(ww)
            
            #order of operations! the A dot xflux.ravel() comes first!

            #- Model image
            submodel = A.dot(xflux.ravel()).reshape(subimg.shape)
            #modeulimage = submodel ?

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

    mean_deltat = np.mean(deltat)
    print("mean deltat %s" %(mean_deltat))

    #- Add extra print because of carriage return \r progress trickery
    if verbose:
        print()

    #+ TODO: what should this do to R in the case of non-uniform bins?
    #+       maybe should do everything in photons/A from the start.            
    #- Convert flux to photons/A instead of photons/bin
    dwave = np.gradient(wavelengths)
    flux /= dwave #this is divide and, divides left operand with the right operand and assign the result to left operand
    ivar *= dwave**2 #similar

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


def ex2d_patch(image, ivar, psf, specmin, nspec, wavelengths, xyrange=None,
         full_output=False, regularize=0.0, ndecorr=False, use_cache=None):
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

    #- Range of image to consider
    waverange = (wavelengths[0], wavelengths[-1])
    specrange = (specmin, specmin+nspec) 
 
    #since xyrange checks to see if we're on the ccd, we cant cache until after this
    if xyrange is None:
        xmin, xmax, ymin, ymax = xyrange = psf.xyrange(specrange, waverange)
        image = image[ymin:ymax, xmin:xmax]
        ivar = ivar[ymin:ymax, xmin:xmax]
    else:
        xmin, xmax, ymin, ymax = xyrange

    nx, ny = xmax-xmin, ymax-ymin
    npix = nx*ny
    
    nspec = specrange[1] - specrange[0]
    nwave = len(wavelengths)

    #- Solve AT W pix = (AT W A) flux
    
    #- Projection matrix and inverse covariance
    #use specter for now, eventually swap out for gpu version
    A = psf.projection_matrix(specrange, wavelengths, xyrange, use_cache=use_cache)

    #- Pixel weights matrix
    w = ivar.ravel()
    #W = spdiags(ivar.ravel(), 0, npix, npix)

    #- Set up the equation to solve (B&S eq 4)
    #get the cpu values too
    #noisyimg_cpu = noisyimg_gpu.get()
    #imgweights_cpu = imgweights_gpu.get()
    #A_cpu = A_gpu.get()

    W = scipy.sparse.spdiags(data=ivar.ravel(), diags=[0,], m=npix, n=npix) #scipy sparse object
    #W_gpu = cpx.scipy.sparse.spdiags(data=imgweights_gpu.ravel(), diags=[0,], m=npix, n=npix)
    #yank gpu back to cpu so we can compare
    #W_yank = W_gpu.get()
    #assert np.allclose(W_cpu.todense(), W_yank.todense()) #todense bc this is a sparse object
    #passes

    ####################################################################################
    #patch specter cleanup in here 

    #-----
    #- Extend A with an optional regularization term to limit ringing.
    #- If any flux bins don't contribute to these pixels,
    #- also use this term to constrain those flux bins to 0.
    
    #- Original: exclude flux bins with 0 pixels contributing
    # ibad = (A.sum(axis=0).A == 0)[0]
    
    #- Identify fluxes with very low weights of pixels contributing            
    fluxweight = W.dot(A).sum(axis=0).A[0]

    # The following minweight is a regularization term needed to avoid ringing due to 
    # a flux bias on the edge flux bins in the
    # divide and conquer approach when the PSF is not perfect
    # (the edge flux bins are constrained only by a few CCD pixels and the wings of the PSF).
    # The drawback is that this is biasing at the high flux limit because bright pixels
    # have a relatively low weight due to the Poisson noise.
    # we set this weight to a value of 1-e4 = ratio of readnoise**2 to Poisson variance for 1e5 electrons 
    # 1e5 electrons/pixel is the CCD full well, and 10 is about the read noise variance.
    # This was verified on the DESI first spectrograph data.
    minweight = 1.e-4*np.max(fluxweight) 
    ibad = fluxweight < minweight
    
    #- Original version; doesn't work on older versions of scipy
    # I = regularize*scipy.sparse.identity(nspec*nwave)
    # I.data[0,ibad] = minweight - fluxweight[ibad]
    
    #- Add regularization of low weight fluxes
    Idiag = regularize*np.ones(nspec*nwave)
    Idiag[ibad] = minweight - fluxweight[ibad]
    I = scipy.sparse.identity(nspec*nwave)
    I.setdiag(Idiag)

    #- Only need to extend A if regularization is non-zero
    if np.any(I.diagonal()):
        pix = np.concatenate( (image.ravel(), np.zeros(nspec*nwave)) )
        Ax = scipy.sparse.vstack( (A, I) )
        wx = np.concatenate( (w, np.ones(nspec*nwave)) )
    else:
        pix = image.ravel()
        Ax = A
        wx = w


    ####################################################################################
    #we now return to our regularly scheduled gpu extraction

    #for now move Ax to the gpu while projection_matrix is still on the cpu
    #and also wx and pix
    Ax_gpu = cpx.scipy.sparse.csr_matrix(Ax)
    wx_gpu = cp.asarray(wx) #better, asarray does not copy
    pix_gpu = cp.asarray(pix)

    #make our new and improved wx using specter cleanup
    Wx_gpu = cpx.scipy.sparse.spdiags(wx_gpu, 0, len(wx_gpu), len(wx_gpu))

    iCov_gpu = Ax_gpu.T.dot(Wx_gpu.dot(Ax_gpu))
    #iCov_cpu = Ax.T.dot(Wx.dot(Ax))
    #yank gpu back to cpu so we can compare
    #iCov_yank = iCov_gpu.get()
    #assert np.allclose(iCov_cpu.todense(), iCov_yank.todense()) #todense bc this is sparse
    #passes

    y_gpu = Ax_gpu.T.dot(Wx_gpu.dot(pix_gpu))
    #y_cpu = Ax.T.dot(Wx.dot(pix))
    #yank gpu back and compare
    #y_yank = y_gpu.get()
    #assert np.allclose(y_cpu, y_yank)
    #passes

    ##we're done with Ax_gpu, let's clear to try to save some memory
    ##may or may not actually do anything to help, i think a lot of this is done automatically
    #del Ax_gpu
    ##same for pix_gpu
    #del pix_gpu
    ##and Wx_gpu
    #del Wx_gpu

    #using instead of spsolve (not currently on the gpu)
    #try again with np.solve and cp.solve
    #cp.linalg.solve
    f_gpu = cp.linalg.solve(iCov_gpu.todense(), y_gpu).reshape((nspec, nwave)) #requires array, not sparse object
    #f_cpu = spsolve(iCov_cpu, y_cpu).reshape((nspec, nwave))
    #f_cpu = np.linalg.solve(iCov_cpu.todense(), y_cpu).reshape((nspec, nwave)) #requires array, not sparse object
    #yank back and compare
    #f_yank = f_gpu.get()
    #assert np.allclose(f_cpu, f_yank)
    #passes

    #numpy and scipy don't agree!
    #assert np.allclose(f_cpu, f_cpu_sp)

    #- Eigen-decompose iCov to assist in upcoming steps
    u_gpu, v_gpu = cp.linalg.eigh(iCov_gpu.todense())
    #u, v = np.linalg.eigh(iCov_cpu.todense())
    #u_cpu = np.asarray(u)
    #v_cpu = np.asarray(v)
    #yank back and compare
    #u_yank = u_gpu.get()
    #v_yank = v_gpu.get()
    #assert np.allclose(u_cpu, u_yank)
    #assert np.allclose(v_cpu, v_yank)
    #passes

    #- Calculate C^-1 = QQ (B&S eq 10)
    d_gpu = cpx.scipy.sparse.spdiags(cp.sqrt(u_gpu), 0, len(u_gpu) , len(u_gpu))
    #d_cpu = scipy.sparse.spdiags(np.sqrt(u_cpu), 0, len(u_cpu), len(u_cpu))
    #yank back and compare
    #d_yank = d_gpu.get()
    #assert np.allclose(d_cpu.todense(), d_yank.todense())
    #passes

    Q_gpu = v_gpu.dot( d_gpu.dot( v_gpu.T ))
    #Q_cpu = v_cpu.dot( d_cpu.dot( v_cpu.T ))
    #yank back and compare
    #Q_yank = Q_gpu.get()
    #assert np.allclose(Q_cpu, Q_yank)
    #passes

    #- normalization vector (B&S eq 11)
    norm_vector_gpu = cp.sum(Q_gpu, axis=1)
    #norm_vector_cpu = np.sum(Q_cpu, axis=1)
    #yank back and compare
    #norm_vector_yank = norm_vector_gpu.get()
    #assert np.allclose(norm_vector_cpu, norm_vector_yank)
    #passes

    #- Resolution matrix (B&S eq 12)
    R_gpu = cp.outer(norm_vector_gpu**(-1), cp.ones(norm_vector_gpu.size)) * Q_gpu
    #R_cpu = np.outer(norm_vector_cpu**(-1), np.ones(norm_vector_cpu.size)) * Q_cpu
    #yank back and compare
    #R_yank = R_gpu.get()
    #assert np.allclose(R_cpu, R_yank)
    #passes

    #- Decorrelated covariance matrix (B&S eq 13-15)
    udiags_gpu = cpx.scipy.sparse.spdiags(1/u_gpu, 0, len(u_gpu), len(u_gpu))
    #udiags_cpu = scipy.sparse.spdiags(1/u_cpu, 0, len(u_cpu), len(u_cpu))
    #yank back and compare
    #udiags_yank = udiags_gpu.get()
    #assert np.allclose(udiags_cpu.todense(),udiags_yank.todense()) #sparse objects
    #passes

    Cov_gpu = v_gpu.dot( udiags_gpu.dot (v_gpu.T ))
    #Cov_cpu = v_cpu.dot( udiags_cpu.dot( v_cpu.T ))
    #yank back and compare
    #Cov_yank = Cov_gpu.get()
    #assert np.allclose(Cov_cpu, Cov_yank)
    #passes

    #OOM here when cusparse tries to allocate memory
    Cx_gpu = R_gpu.dot(Cov_gpu.dot(R_gpu.T))
    #Cx_cpu = R_cpu.dot(Cov_cpu.dot(R_cpu.T))
    #yank back and compare
    #Cx_yank = Cx_gpu.get()
    #assert np.allclose(Cx_cpu, Cx_yank)
    #passes

    #- Decorrelated flux (B&S eq 16)
    fx_gpu = R_gpu.dot(f_gpu.ravel()).reshape(f_gpu.shape)
    #fx_cpu = R_cpu.dot(f_cpu.ravel()).reshape(f_cpu.shape)
    #yank back and compare
    #fx_yank = fx_gpu.get()
    #assert np.allclose(fx_cpu, fx_yank)
    #passes

    #- Variance on f (B&S eq 13)
    #in specter fluxivar = norm_vector**2 ??
    #varfx_gpu = cp.diagonal(Cx_gpu)
    #varfx_cpu = np.diagonal(Cx_cpu).reshape((nspec, nwave))
    varfx_gpu = (norm_vector_gpu * 2).reshape((nspec, nwave))
    #varfx_cpu = (norm_vector_cpu * 2).reshape((nspec, nwave)) #let's try it 
    #yank back and compare
    #varfx_yank = varfx_gpu.get()
    #assert np.allclose(varfx_cpu, varfx_yank)
    #passes

    #pull back to cpu to return to ex2d
    flux = fx_gpu.get()
    ivar = varfx_gpu.get()
    R = R_gpu.get()
    xflux = f_gpu.get()
    #A is on the cpu for now
    iCov = iCov_gpu.get()

    if full_output:
        results = dict(flux=flux, ivar=ivar, R=R, xflux=xflux, A=A, iCov=iCov)
        results['options'] = dict(
            specmin=specmin, nspec=nspec, wavelengths=wavelengths,
            xyrange=xyrange, regularize=regularize, ndecorr=ndecorr
            )
        return results
    else:
        return flux, ivar, R

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
