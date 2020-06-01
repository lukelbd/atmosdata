#!/usr/bin/env python3
"""
Wrappers that permit duck-type array input to various functions.
"""
import re
import functools
import itertools
import numpy as np
import xarray as xr
import pint.util as putil
from .. import ureg
from . import warnings

# Regex to find terms surrounded by curly braces that can be filled with str.format()
REGEX_FORMAT = re.compile(r'\{([^{}]+?)\}')  # '+?' is non-greedy, group inside brackets


def _xarray_xy_wrapper(func):
    """
    Simple wrapper that permits passing either two vectors or a dataarray
    to the function. In the latter case coordinates are inferred from the
    array along axis `axis` or dimension `dim` (keyword args).

    Example
    -------

    >>> import numpy as np
    ... import xarray as xr
    ... x = np.arange(5)
    ... y = np.random.rand(5, 6, 7)
    ... data = xr.DataArray(y, dims=('x',), coords={'x': x})
    ... func(x, y, **kwargs)
    ... func(data, **kwargs)

    """
    def wrapper(*args, keep_attrs=False, **kwargs):
        # Transform DataArray input
        is_dataarray = len(args) == 1 and isinstance(args[0], xr.DataArray)
        if is_dataarray:
            # Translate 'dim' arguments into axis number
            dim = kwargs.pop('dim', None)
            axis = kwargs.get('axis', None)
            if dim is not None and axis is not None:
                warnings._warn_climpy('Ambiguous axis specification.')
                dim = None

            # Get data and coordinates
            input = args[0]
            if axis is not None:
                dim = input.dims[axis]
            try:  # apply units! TODO: add 'data' attribute
                args = (input.phys.coords[dim].data, input.phys.data)
            except AttributeError:
                args = (input.coords[dim].data, input.data)

        # Call main function
        result = func(*args, **kwargs)

        # Add back metadata. Function should return 'y' value of coordinates
        # unchanged or 'x', 'y' pair if coordinates changed.
        if is_dataarray:
            # Repair output coordinates
            output = result
            coords = dict(input.coords)
            if isinstance(result, tuple):  # return value of deriv_half
                coord, output = result
                attrs = {}
                if hasattr(coord, 'units'):
                    attrs['units'] = format(coord.units, '~')  # short-form units
                coord = xr.DataArray(coord, name=dim, dims=(dim,), attrs=attrs)
                coords[dim] = coord

            # Create output array
            attrs = {}
            if keep_attrs:
                attrs = input.attrs.copy()
            result = xr.DataArray(output, dims=input.dims, coords=coords, attrs=attrs)

        return result

    return wrapper


def _pint_wrapper(units_in, units_out, strict=False, **fmt_defaults):
    """
    Handle pint units, similar to `~pint.UnitRegistry.wraps`. Put input units
    as the first argument, set `strict` to ``False`` by default, and if
    non-quantities are passed into the function, ensure non-quantities are returned
    by the function rather than a quantity with ``'dimensionless'`` units, and
    default to ``strict=False``.

    Parameters
    ----------
    units_in : unit-like, string, or list thereof
        A pint unit like `~pint.UnitRegistry.meter`, a relational string
        like ``'=x ** 2'``, or list thereof, specifying the units of the
        input data. You can put keyword arguments into the unit specification
        with, for example ``_pint_wrapper(('=x', '=y'), '=y / x ** {{n}}')``
        for the ``n``th derivative.
    units_out : unit-like, string, or list thereof
        As with `units_in` but for the output arguments.
    strict : bool, optional
        Whether non-relational (absolute) unit specifications are strict.
        If ``False``, non-quantity input is cast to the default units.
    **fmt_defaults
        Default values for the terms surrounded by curly braces in relational
        or string unit specifications.

    Example
    -------
    Here is a simple example for a derivative wrapper.

    >>> import climpy
    ... from climpy.internals import quack
    ... ureg = climpy.ureg
    ... @quack._pint_wrapper(('=x', '=y'), '=y / x ** {order}', order=1)
    ... def deriv(x, y, order=1):
    ...     return y / x ** order
    ... deriv(1 * ureg.m, 1 * ureg.s, order=2)
    1.0 <Unit('second / meter ** 2')>

    """
    # Handle singleton input or multiple input
    is_container_in = isinstance(units_in, (tuple, list))
    is_container_out = isinstance(units_out, (list, tuple))
    if not is_container_in:
        units_in = (units_in,)
    if not is_container_out:
        units_out = (units_out,)

    # Ensure valid kwargs
    # NOTE: Pint cannot handle singleton-tuple of return value unit specifications.
    # So when passing to wrapper simply expand singleton tuples.
    fmt_args = []
    for units in (units_in, units_out):
        for unit in units:
            if isinstance(unit, str):
                fmt_args.extend(REGEX_FORMAT.findall(unit))
    if set(fmt_args) != set(fmt_defaults):
        raise ValueError(
            f'Invalid or insufficient keyword args {tuple(fmt_defaults)} '
            f'when string unit specification includes terms {tuple(fmt_args)}.'
        )

    # Ensure valid args unit specifications
    for arg in units_in:
        if arg is not None and not isinstance(arg, (ureg.Unit, str)):
            raise TypeError(
                f'Wraps arguments must by of type str or Unit, not {type(arg)} ({arg}).'
            )

    # Ensure valid return unit specifications
    for arg in units_out:
        if arg is not None and not isinstance(arg, (ureg.Unit, str)):
            raise TypeError(
                "Wraps 'units_out' argument must by of type str or Unit, "
                f'not {type(arg)} ({arg}).'
            )

    # Parse input
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            # Fill parameters inside units
            units_in_fmt = []
            units_out_fmt = []
            for units, units_fmt in zip(
                (units_in, units_out), (units_in_fmt, units_out_fmt)
            ):
                for unit in units:
                    if isinstance(unit, str):
                        # Get format values from user input keyword args
                        fmt_keys = REGEX_FORMAT.findall(unit)
                        fmt_kwargs = {
                            key: value for key, value in kwargs.items()
                            if key in fmt_keys
                        }
                        # Fill missing format values and format string
                        for key, value in fmt_defaults.items():
                            fmt_kwargs.setdefault(key, value)
                        unit = unit.format(**fmt_kwargs)
                    # Add new unit string
                    units_fmt.append(unit)

            # Dequantify input
            converter = _parse_pint_args(units_in_fmt)
            args_new, args_by_name = converter(args, strict)

            # Call main function
            result = func(*args_new, **kwargs)
            if not is_container_out:
                result = (result,)

            # Quantify output, but *only* if input was quantities
            pairs = tuple(_to_units_container(arg) for arg in units_out_fmt)
            no_quantities = not any(isinstance(arg, ureg.Quantity) for arg in args)
            units = tuple(
                _replace_units(unit, args_by_name) if is_ref else unit
                for (unit, is_ref) in pairs
            )
            result = tuple(
                res if unit is None or no_quantities else ureg.Quantity(res, unit)
                for unit, res in itertools.zip_longest(units, result)
            )

            # Return sanitized values
            if not is_container_out:
                result = result[0]
            return result

        return wrapper

    return decorator


def _parse_pint_args(args):
    """
    Parse pint wrapper arguments.
    """
    # Helper variables
    defs_args = set()  # arguments which contain definitions
    defs_args_ndx = set()  # (i.e. names that appear alone and for the first time)
    dependent_args_ndx = set()  # arguments which depend on others
    unit_args_ndx = set()  # arguments which have units.
    args_as_uc = [_to_units_container(arg) for arg in args]

    # Check for references in args, remove None values
    for ndx, (arg, is_ref) in enumerate(args_as_uc):
        if arg is None:
            continue
        elif is_ref:
            if len(arg) == 1:
                [(key, value)] = arg.items()
                if value == 1 and key not in defs_args:
                    # This is the first time that
                    # a variable is used => it is a definition.
                    defs_args.add(key)
                    defs_args_ndx.add(ndx)
                    args_as_uc[ndx] = (key, True)
                else:
                    # The variable was already found elsewhere,
                    # we consider it a dependent variable.
                    dependent_args_ndx.add(ndx)
            else:
                dependent_args_ndx.add(ndx)
        else:
            unit_args_ndx.add(ndx)

    # Check that all valid dependent variables
    for ndx in dependent_args_ndx:
        arg, is_ref = args_as_uc[ndx]
        if not isinstance(arg, dict):
            continue
        if not set(arg.keys()) <= defs_args:
            raise ValueError(
                'Found a missing token while wrapping a function: '
                f'Not all variable referenced in {args[ndx]} are defined!'
            )

    # Generate converter
    def _converter(args, strict):
        args_new = list(args)
        args_by_name = {}

        # First pass: Grab named values
        for ndx in defs_args_ndx:
            arg = args[ndx]
            args_by_name[args_as_uc[ndx][0]] = arg
            args_new[ndx] = getattr(arg, '_magnitude', arg)

        # Second pass: calculate derived values based on named values
        for ndx in dependent_args_ndx:
            arg = args[ndx]
            assert _replace_units(args_as_uc[ndx][0], args_by_name) is not None
            args_new[ndx] = ureg._convert(
                getattr(arg, '_magnitude', arg),
                getattr(arg, '_units', putil.UnitsContainer({})),
                _replace_units(args_as_uc[ndx][0], args_by_name),
            )

        # Third pass: convert other arguments
        for ndx in unit_args_ndx:
            if isinstance(args[ndx], ureg.Quantity):
                args_new[ndx] = ureg._convert(
                    args[ndx]._magnitude, args[ndx]._units, args_as_uc[ndx][0]
                )
            elif strict:
                if isinstance(args[ndx], str):
                    # If the value is a string, we try to parse it
                    tmp_value = ureg.parse_expression(args[ndx])
                    args_new[ndx] = ureg._convert(
                        tmp_value._magnitude, tmp_value._units, args_as_uc[ndx][0]
                    )
                else:
                    raise ValueError(
                        'A wrapped function using strict=True requires '
                        'quantity or a string for all arguments with not None units. '
                        f'(error found for {args_as_uc[ndx][0]}, {args_new[ndx]})'
                    )

        return args_new, args_by_name

    return _converter


def _replace_units(original_units, values_by_name):
    """
    Convert a unit compatible type to a UnitsContainer.
    """
    q = 1
    with np.errstate(divide='ignore', invalid='ignore'):
        # NOTE: Multiply quantities successively here just to pull out units
        # after everything is done. But avoid broadcasting errors as shown.
        for arg_name, exponent in original_units.items():
            q = q * values_by_name[arg_name] ** exponent
            q = getattr(q, 'magnitude', q).flat[0] * getattr(q, 'units', 1)
    return getattr(q, '_units', putil.UnitsContainer({}))


def _to_units_container(arg):
    """
    Convert a unit compatible type to a UnitsContainer, checking if it is string
    field prefixed with an equal (which is considered a reference). Return the
    unit container and a boolean indicating whether this is an equal string.
    """
    if isinstance(arg, str) and '=' in arg:
        return putil.to_units_container(arg.split('=', 1)[1]), True
    return putil.to_units_container(arg, ureg), False