# Configuration for a tight spread on main deflector with PEMD + SHEAR, sources
# drawn from COSMOS, and only varying the d_los and sigma_sub of the DG_19
# subhalo and los classes

import numpy as np
from scipy.stats import norm, truncnorm, uniform
from paltas.Substructure.los_dg19 import LOSDG19
from paltas.Substructure.subhalos_dg19 import SubhalosDG19
from paltas.MainDeflector.simple_deflectors import PEMDShear
from paltas.Sources.sersic import SingleSersicSource
from lenstronomy.Util.kernel_util import degrade_kernel
from paltas.Sampling import distributions
from astropy.io import fits
import pandas as pd
import paltas
import os

# Define the numerics kwargs.
kwargs_numerics = {'supersampling_factor':2,'supersampling_convolution':True}
# We do not use point_source_supersampling_factor but it must be passed in to
# surpress a warning.
kwargs_numerics['point_source_supersampling_factor'] = (
    kwargs_numerics['supersampling_factor'])
# This is always the number of pixels for the CCD. If drizzle is used, the
# final image will be larger.
numpix = 128

# Define some general image kwargs for the dataset
mask_radius = 0.0
mag_cut = 2.0

# Define arguments that will be used multiple times
output_ab_zeropoint = 25.127

config_dict = {
    'subhalo':{
        'class': SubhalosDG19,
        'parameters':{
            'sigma_sub':0.0e-3,
            'shmf_plaw_index':-1.98,
            'm_pivot': 1e10,
            'm_min': 7e7,
            'm_max': 1e10,
            'c_0':18,
            'conc_zeta':-0.2,
            'conc_beta':0.8,
            'conc_m_ref': 1e8,
            'k1':0.0,
            'k2':0.0,
            'dex_scatter': 1.0
        }
    },
    'main_deflector':{
        'class': PEMDShear,
        'parameters':{
            'M200': 1e13,
            'z_lens': 0.5,
            'gamma': 2.0,
            'theta_E': 1.1,
            'e1': (1.0-0.9)/(1.0+0.9),
            'e2': 0.0,
            'center_x': 0.08,
            'center_y': -0.16,
            'gamma1': 0.0,
            'gamma2': 0.0,
            'ra_0':0.0, 
            'dec_0':0.0
        }
    },
    'source':{
        'class': SingleSersicSource,
        'parameters':{
            'magnitude': -29.027384789, # Equivalent to an amplitude of 10.
            'output_ab_zeropoint': output_ab_zeropoint,
            'R_sersic': 1.5,
            'n_sersic':3.0 ,
            'e1': (1.0-0.9)/(1.0+0.9),
            'e2': 0.0,
            'center_x': 0.16,
            'center_y': -0.08,
            'z_source':1.5}
    },
    'cosmology':{
        'parameters':{
            'cosmology_name': 'planck18'
        }
    },
    'psf':{
        'parameters':{
            'psf_type':'GAUSSIAN',
            'fwhm': 0.04,
            'point_source_supersampling_factor':2
        }
    },
    'detector':{
        'parameters':{
            'pixel_scale':0.040,'ccd_gain':1.58,'read_noise':3.0,
            'magnitude_zero_point':output_ab_zeropoint,
            'exposure_time':1380,'sky_brightness':21.83,
            'num_exposures':1,'background_noise':None
        }
    }
}
