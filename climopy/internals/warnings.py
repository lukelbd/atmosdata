#!/usr/bin/env python3
"""
Warnings used internally by climo.
"""
import re
import sys
import warnings

import pint
import xarray as xr

warnings.simplefilter('error', category=pint.UnitStrippedWarning)
warnings.simplefilter('ignore', category=xr.core.extensions.AccessorRegistrationWarning)
ClimoPyWarning = type('ClimoPyWarning', (UserWarning,), {})


def _warn_climopy(message):
    """
    Emit a `ClimoPyWarning` and try to show the stack level corresponding
    to user code by jumping to the stack outside of climopy, numpy, scipy,
    pandas, xarray, pint, and scipy.
    """
    frame = sys._getframe()
    stacklevel = 1
    while True:
        if frame is None:
            break  # when called in embedded context may hit frame is None
        if not re.match(
            r'\A(climopy|numpy|scipy|xarray|pandas|pint).',
            frame.f_globals.get('__name__', '')
        ):
            break
        frame = frame.f_back
        stacklevel += 1
    warnings.warn(message, ClimoPyWarning, stacklevel=stacklevel)
