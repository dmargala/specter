#!/usr/bin/env python

"""
1D Extraction like Horne 1986

Stephen Bailey, LBL
Spring 2013
"""

import sys
import os
import numpy as N
import math

from specter.util import gausspix, weighted_solve

def extract1d(img, ivar, psf, specrange=None, yrange=None,
              nspec_per_group=20):
    """
    Extract spectra from an image using row-by-row weighted extraction.
    
    Inputs:
        img[ny, nx]     CCD image
        ivar[ny, nx]    Image inverse variance
        psf object
        
    Optional Inputs:
        specrange = (specmin, specmax) Spectral range to extract (default all)
        yrange = (ymin, ymax) CCD y (row) range to extract (default all rows)
        --> ranges are python-like, i.e. yrange=(0,100) extracts 100 rows
            from 0 to 99 inclusive but not row 100.
            
        groupspec: extract spectra in groups of N spectra
            (faster if spectra are physically separated into non-overlapping
            groups)
        
    Returns:
        spectra[nspec, ny]   - extracted spectra
        specivar[nspec, ny]  - inverse variance of spectra  
    """

    #- Range of spectra to extract
    specmin, specmax = specrange if (specrange is not None) else (0, psf.nspec)        
    nspec = specmax - specmin
    
    #- Rows to extract
    ymin, ymax = yrange if (yrange is not None) else (0, psf.npix_y)
    ny = ymax - ymin
    nx = img.shape[0]
    xx = N.arange(nx)
    
    spectra = N.zeros((nspec, ny))
    specivar = N.zeros((nspec, ny))
        
    #- Loop over groups of spectra
    for speclo in range(specmin, specmax, nspec_per_group):
        spechi = min(specmax, speclo+nspec_per_group)
                
        #- Loop over CCD rows
        for row in range(ymin, ymax):
            if row%500 == 0:
                print "Row %3d spectra %d:%d" % (row, speclo, spechi)
        
            #- Determine x range covered for this row of this group of spectra
            wlo = psf.wavelength(speclo, y=row)
            whi = psf.wavelength(spechi-1, y=row)
            if speclo == 0:
                xmin = 0
            else:
                xmin = int(0.5*(psf.x(speclo-1, wlo) + psf.x(speclo, wlo)))
        
            if spechi >= psf.nspec:
                xmax = psf.npix_x
            else:
                xmax = int(0.5*(psf.x(spechi-1, wlo) + psf.x(spechi, wlo)) + 1)
                
            #- Design matrix for pixels = A * flux for this row
            A = N.zeros( (xmax-xmin, spechi-speclo) )
            for ispec in range(speclo, spechi):
                w = psf.wavelength(ispec, y=row)
                x0 = psf.x(ispec, w)
                xsigma = psf.xsigma(ispec, w)
            
                #- x range for single spectrum on single row
                xlo = max(xmin, int(x0-5*xsigma))
                xhi = min(xmax, int(x0+5*xsigma+1))
            
                A[xlo-xmin:xhi-xmin, ispec-speclo] = gausspix(xx[xlo:xhi], x0, xsigma)
            
            #- Solve
            tmpspec, iCov = weighted_solve(A, img[row, xmin:xmax], ivar[row, xmin:xmax])
        
            #- TODO: Should repeat extraction using model to derive errors
            #- Requires read noise as an input parameter
        
            spectra[speclo-specmin:spechi-specmin, row-ymin] = tmpspec
            specivar[speclo-specmin:spechi-specmin, row-ymin] = iCov.diagonal()            
                
    return spectra, specivar
    
        