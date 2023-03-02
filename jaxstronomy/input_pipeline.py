# coding=utf-8

# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Code for drawing batches of example images used to train a neural
network.
"""

import functools
from typing import Any, Mapping, Sequence, Tuple, Union

import jax.numpy as jnp
import jax

from jaxstronomy import cosmology_utils
from jaxstronomy import los
from jaxstronomy import subhalos
from jaxstronomy import image_simulation
from jaxstronomy import utils


def encode_normal(mean: float, std: float) -> jnp.ndarray:
    """Generate the jax array that encodes a normal distribution.

    Args:
        mean: Mean of the normal distribution.
        std: Standard deviation of the normal distribution.

    Returns:
        Encoding that represents a normal with given mean and standard
            deviation.
    """

    # Encoding is currently [uniform_bool, unfirom_min, uniform_max,
    # normal_bool, normal_mean, normal_std, constant].
    return jnp.array([0.0, 0.0, 0.0, 1.0, mean, std, 0.0])


def encode_uniform(minimum: float, maximum: float) -> jnp.ndarray:
    """Generate the jax array that encodes a uniform distribution.

    Args:
        min: Minimum value of the uniform distribution.
        max: Maximum value of the uniform distribution.

    Returns:
        Encoding that represents a uniform with given min and max.
    """

    # Encoding is currently [uniform_bool, unfirom_min, uniform_max,
    # normal_bool, normal_mean, normal_std, constant].
    return jnp.array([1.0, minimum, maximum, 0.0, 0.0, 0.0, 0.0])


def encode_constant(constant: float) -> jnp.ndarray:
    """Generate the jax array that encodes a constant value.

    Args:
        constant: Constant value.

    Returns:
        Encoding that represents a constant value.
    """
    # Encoding is currently [uniform_bool, unfirom_min, uniform_max,
    # normal_bool, normal_mean, normal_std, constant].
    return jnp.array([0.0, 0.0, 0.0, 0.0, 0.0, 0.0, constant])


def decode_maximum(encoding: jnp.ndarray) -> float:
    """Decode the maximum value of the distribution defined by encoding.

    Args:
        encoding: Encoded distribution

    Returns:
        Maximum value of encoded distribution. May be an approximation for
        distributions without a maximum.
    """
    # Encoding is currently [uniform_bool, unfirom_min, uniform_max,
    # normal_bool, normal_mean, normal_std, constant].
    # If not uniform will give 0.
    maximum = encoding[0] * encoding[2]
    # If normal will give mean + five sigma.
    maximum += encoding[3] * (encoding[4] + encoding[5]*5)
    # If constant return constant
    maximum += encoding[6]
    return maximum


def decode_minimum(encoding: jnp.ndarray) -> float:
    """Decode the minimum value of the dsitribution defined by the encoding.

    Args:
        encoding: Encoded distribution

    Returns:
        Minimum value of encoded distribution. May be an approximation for
        distributions without a minimum.
    """
    # Encoding is currently [uniform_bool, unfirom_min, uniform_max,
    # normal_bool, normal_mean, normal_std, constant].
    # If not uniform will give 0.
    minimum = encoding[0] * encoding[1]
    # If normal will give mean - five sigma.
    minimum += encoding[3] * (encoding[4] - encoding[5]*5)
    # If constant return constant
    minimum += encoding[6]
    return minimum


def draw_from_encoding(encoding: jnp.ndarray, rng: Sequence[int]) -> float:
    """Draw from encoded distribution.

    Args:
        encoding: Encoding defining the distribution.
        rng: jax PRNG key.

    Returns:
        A draw from the encoded distribution.
    """
    # Encoding is currently [uniform_bool, unfirom_min, uniform_max,
    # normal_bool, normal_mean, normal_std, constant].
    # Start with the uniform component.
    draw = encoding[0] * (
        jax.random.uniform(rng) * (encoding[2] - encoding[1]) + encoding[1])
    # Now the normal component.
    draw += encoding[3] * (jax.random.normal(rng) * encoding[5] + encoding[4])
    # And finally the constant
    draw += encoding[6]
    return draw


def normalize_param(parameter: float, encoding: jnp.ndarray) -> float:
    """Return parameter normalized by encoded distribution.

    Args:
        parameter: Parameter value to normalize
        encoding: Encoding defining the distribution.

    Returns:
        Normalized parameter.
    """
    # Encoding is currently [uniform_bool, unfirom_min, uniform_max,
    # normal_bool, normal_mean, normal_std, constant].
    # Normalize uniform distribution to be between 0 and 1.
    normalized_param = jax.lax.select(
        encoding[0] > 0.0,
        (parameter - encoding[1]) / (encoding[2] - encoding[1]), 0.0)
    # Normalize normal distribution to mean 0 and standard deviation 1.0
    normalized_param += jax.lax.select(
        encoding[3] > 0.0,(parameter - encoding[4]) / encoding[5], 0.0)
    # Constant will be normalzied to 0.0 by default.

    return normalized_param

def generate_grids(
        config: Mapping[str, Mapping[str, jnp.ndarray]]
) -> Tuple[jnp.ndarray, jnp.ndarray]:
    """Generate the x- and y-grid on which to generate the lensing image.

    Args:
        config: Configuration dictionary from which the detector kwargs will be
            drawn.

    Returns:
        x- and y-grid in units of arcseconds as a tuple.
    """
    kwargs_detector = config['kwargs_detector']
    grid_x, grid_y = utils.coordinates_evaluate(kwargs_detector['n_x'],
        kwargs_detector['n_y'], kwargs_detector['pixel_width'],
        kwargs_detector['supersampling_factor'])
    return grid_x, grid_y


def intialize_cosmology_params(
        config: Mapping[str, Mapping[str, jnp.ndarray]], rng: Sequence[int],
) -> Mapping[str, Union[float, int, jnp.ndarray]]:
    """Initialize the cosmology parameters as needed by the config.

    Args:
        config: Configuration dictionary for input generation.
        rng: jax PRNG key.

    Returns:
        Cosmological parameters with appropriate lookup table.
    """
    max_source_z = decode_maximum(
        config['lensing_config']['source_params']['z_source'])
    dz = decode_minimum(config['lensing_config']['los_params']['dz'])
    m_max = max(
        decode_maximum(config['lensing_config']['subhalo_params']['m_max']),
        decode_maximum(config['lensing_config']['los_params']['m_max']))
    m_min = min(
        decode_minimum(config['lensing_config']['subhalo_params']['m_min']),
        decode_minimum(config['lensing_config']['los_params']['m_min']))

    cosmology_params_init = draw_sample(config['cosmology_params'], rng)

    # Initial bounds on lagrangian radius are just placeholders.
    cosmology_params = cosmology_utils.add_lookup_tables_to_cosmology_params(
        cosmology_params_init, max_source_z, dz / 2, 1e-4, 1e3, 2)
    r_min = cosmology_utils.lagrangian_radius(cosmology_params,
                                              m_min / 10)
    r_max = cosmology_utils.lagrangian_radius(cosmology_params,
                                              m_max * 10)
    cosmology_params = cosmology_utils.add_lookup_tables_to_cosmology_params(
        cosmology_params_init, 1.5, dz / 2, r_min, r_max, 10000)
    extrenal_los_params = {'m_min': m_min, 'm_max': m_max, 'dz': dz}
    return los.add_los_lookup_tables_to_cosmology_params(
        extrenal_los_params, cosmology_params, max_source_z)


def draw_sample(
        encoded_configuration: Mapping[str, Mapping[str, float]],
        rng: Sequence[int]
) -> Mapping[str, Mapping[str, float]]:
    """Map an econded configuration into a configuration of randomly draws.

    Args:
        encoded_configuration: Configuration with encoded distribution for all
            leaves of the PyTree structure.
        rng: jax PRNG key.

    Returns:
        Configuration with encoded distribution replaced by draws from those
            distributions.
    """
    # Generate the rng keys we will need for each leaf.
    treedef = jax.tree_util.tree_structure(encoded_configuration)
    rng_keys = jax.random.split(rng, treedef.num_leaves)
    rng_tree = jax.tree_util.tree_unflatten(treedef, rng_keys)
    return jax.tree_util.tree_map(draw_from_encoding, encoded_configuration,
                                  rng_tree)


def extract_multiple_models(
        encoded_configuration: Mapping[str, Mapping[str, float]],
        rng: Sequence[int], n_models: int
) -> Mapping[str, jnp.ndarray]:
    """Extract multiple models from a single configuration.

    Args:
        encoded_configuration: Encodings for each of the parameters of the
            model(s).
        rng: jax PRNG key.
        n_models: Number of models to draw parameters for.

    Returns:
        Draws for the parameters for all the models. For each parameters, first
        dimension will be the number of models.
    """
    draw_sample_vmap = jax.vmap(draw_sample, in_axes=[None, 0])
    rng_list = jax.random.split(rng, n_models)
    draws = draw_sample_vmap(encoded_configuration, rng_list)
    # Add the model index list.
    draws['model_index'] = jnp.arange(n_models)
    return draws


def extract_truth_values(
        all_params: Mapping[str, Mapping[str, jnp.ndarray]],
        lensing_config: Mapping[str, Mapping[str, jnp.ndarray]],
        truth_parameters: Tuple[Sequence[str], Sequence[str]]) -> jnp.ndarray:
    """Extract the truth parameters and normalize them according to the config.

    Args:
        all_params: All of the parameters grouped by object.
        lensing_config: Distribution encodings for each of the parameters.
        truth_parameters: List of the lensing objects and corresponding
            parameters to extract.

    Returns:
        Truth values for each of the requested parameters.
    """
    extract_objects, extract_keys = truth_parameters
    return jnp.array(jax.tree_util.tree_map(
        lambda x, y: normalize_param(all_params[x][y],
                                     lensing_config[x][y]),
        extract_objects, extract_keys))


def draw_image_and_truth(
        lensing_config: Mapping[str, Mapping[str, jnp.ndarray]],
        cosmology_params: Mapping[str, Union[float, int, jnp.ndarray]],
        grid_x: jnp.ndarray, grid_y: jnp.ndarray, rng: Sequence[int],
        all_models: Mapping[str, Sequence[Any]],
        principal_md_index: int, principal_source_index: int,
        kwargs_simulation: Mapping[str, int],
        kwargs_detector:  Mapping[str, Union[int, float]],
        kwargs_psf: Mapping[str, Union[float, int, jnp.ndarray]],
        truth_parameters: Tuple[Sequence[str], Sequence[str]]
) -> Tuple[jnp.ndarray, jnp.ndarray]:
    """Draw image and truth values for a realization of the lensing config.

    Args:
        lensing_config: Distribution encodings for each of the objects in the
            lensing system.
        cosmology_params: Cosmological parameters that define the universe's
            expansion.
        grid_x: x-grid in units of arcseconds.
        grid_y: y-grid in units of arcseconds.
        rng: jax PRNG key.
        all_models: Tuple of model classes to consider for each component.
        principal_md_index: Index of the main deflector model to consider when
            determining the position of the source and substructure.
        principal_source_index: Index of the source model to consider when
            determining the position of the substructure.
        kwargs_simulation: Keyword arguments for the draws of the substructure.
        kwargs_detector: Keyword arguments defining the detector configuration.
        kwargs_psf: Keyword arguments defining the point spread function. The
            psf is applied in the supersampled space, so the size of pixels
            should be defined with respect to the supersampled space.
        truth_parameters: List of the lensing objects and corresponding
            parameters to extract.

    Returns:
        Image and corresponding truth values.

    Notes:
        To jit compile, every parameter after rng must be fixed.
    """

    num_z_bins = kwargs_simulation['num_z_bins']
    los_pad_length = kwargs_simulation['los_pad_length']
    subhalos_pad_length = kwargs_simulation['subhalos_pad_length']
    sampling_pad_length = kwargs_simulation['sampling_pad_length']

    rng_md, rng_source, rng_ll, rng_los, rng_sub, rng = jax.random.split(rng, 6)
    main_deflector_params = extract_multiple_models(
        lensing_config['main_deflector_params'], rng_md,
        len(all_models['all_main_deflector_models'])
    )
    source_params = extract_multiple_models(
        lensing_config['source_params'], rng_source,
        len(all_models['all_source_models'])
    )
    lens_light_params = extract_multiple_models(
        lensing_config['lens_light_params'], rng_ll,
        len(all_models['all_source_models'])
    )
    los_params = draw_sample(lensing_config['los_params'], rng_los)
    subhalo_params = draw_sample(lensing_config['subhalo_params'], rng_sub)

    # Extract the principle model for redshifts and substructure draws.
    main_deflector_params_sub = jax.tree_util.tree_map(
        lambda x: x[principal_md_index], main_deflector_params
    )
    source_params_sub = jax.tree_util.tree_map(
        lambda x: x[principal_source_index], source_params
    )
    lens_light_params_sub = jax.tree_util.tree_map(
        lambda x: x[principal_source_index], lens_light_params
    )

    # Repackage the parameters.
    all_params = {
        'source_params': source_params_sub,
        'lens_light_params': lens_light_params_sub,
        'los_params': los_params, 'subhalo_params': subhalo_params,
        'main_deflector_params': main_deflector_params_sub
    }

    rng_los, rng_sub = jax.random.split(rng)
    los_before_tuple, los_after_tuple = los.draw_los(
        main_deflector_params_sub, source_params_sub, los_params,
        cosmology_params, rng_los, num_z_bins, los_pad_length)
    subhalos_z, subhalos_kwargs = subhalos.draw_subhalos(
        main_deflector_params_sub, source_params_sub, subhalo_params,
        cosmology_params, rng_sub, subhalos_pad_length, sampling_pad_length)

    kwargs_lens_all = {
        'z_array_los_before': los_before_tuple[0],
        'kwargs_los_before': los_before_tuple[1],
        'z_array_los_after': los_after_tuple[0],
        'kwargs_los_after': los_after_tuple[1],
        'kwargs_main_deflector': main_deflector_params,
        'z_array_main_deflector': main_deflector_params['z_lens'],
        'z_array_subhalos': subhalos_z, 'kwargs_subhalos': subhalos_kwargs}
    z_source = source_params_sub['z_source']

    image_supersampled = image_simulation.generate_image(
        grid_x, grid_y, kwargs_lens_all, source_params,
        lens_light_params, kwargs_psf, cosmology_params, z_source,
        kwargs_detector, all_models)
    image = utils.downsample(image_supersampled,
                             kwargs_detector['supersampling_factor'])
    # Normalize and the image to have standard deviation 1.
    image /= jnp.std(image)

    # Extract the truth values and normalize them.
    truth = extract_truth_values(all_params, lensing_config, truth_parameters)

    return image, truth