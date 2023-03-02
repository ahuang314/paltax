# coding=utf-8
#
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
"""Training script for dark matter substructure inference."""

import functools
import itertools
import time
from typing import Any, Iterator, Mapping, Optional, Sequence, Tuple, Union

from absl import app
from absl import flags

from clu import metric_writers
from clu import periodic_actions
from flax import jax_utils
from flax.training import checkpoints
from flax.training import common_utils
from flax.training import train_state
import jax
from jax import lax
import jax.numpy as jnp
import ml_collections
import optax

from jaxstronomy import input_pipeline
from jaxstronomy import models


FLAGS = flags.FLAGS
flags.DEFINE_string('workdir', None, 'working directory')
flags.DEFINE_float('learning_rate', 0.001, 'learning rate')
flags.DEFINE_integer('num_unique_batches', 0,
    'number of unique batches of data to draw. If 0 produces infinite data.')


def initialized(
        rng: Sequence[int], image_size: int, model: Any
    ) -> Tuple[Any, Any]:
    """Initialize the model parameters

    Args:
        rng: jax PRNG key.
        image_size: Size of the input image.
        model: Model class to initialize.

    Returns:
        Initialized model parameters and batch stats.
    """
    input_shape = (1, image_size, image_size, 1)
    @jax.jit
    def init(*args):
        return model.init(*args)
    variables = init({'params': rng}, jnp.ones(input_shape, model.dtype))
    return variables['params'], variables['batch_stats']


def gaussian_loss(outputs: jnp.ndarray, truth: jnp.ndarray) -> jnp.ndarray:
    """Gaussian loss calculated on the outputs and truth values.

    Args:
        outputs: Values outputted by the model.
        truth: True value of the parameters.

    Returns:
        Gaussian loss.

    Notes:
        Loss does not inlcude constant factor of 1 / (2 * pi) ^ (d/2)
    """
    mean, log_var = jnp.split(outputs, 2, axis=-1)
    loss = 0.5 * jnp.sum(
        jnp.multiply(jnp.square(mean-truth), jnp.exp(-log_var)), axis=-1)
    loss += 0.5*jnp.sum(log_var, axis=-1)
    return jnp.mean(loss)


def compute_metrics(
        outputs: jnp.ndarray, truth: jnp.ndarray) -> Mapping[str, jnp.ndarray]:
    """Compute the performance metrics of the output.

    Args:
        outputs: Values outputted by the model.
        truth: True value of the parameters.

    Returns:
        Value of each of the metrics.
    """
    loss = gaussian_loss(outputs, truth)
    mean, _ = jnp.split(outputs, 2, axis=-1)
    rmse = jnp.sqrt(jnp.mean(jnp.square(mean - truth)))
    metrics = {
        'loss': loss,
        'rmse': rmse,
    }
    metrics = lax.pmean(metrics, axis_name='batch')
    return metrics


def get_learning_rate_schedule(
        config: ml_collections.ConfigDict,
        base_learning_rate: float) -> Any:
    """Return the learning rate schedule function.

    Args:
        config: Training configuration.
        base_learning_rate: Base learning rate for the schedule.

    Returns:
        Mapping from step to learning rate according to the schedule.
    """
    warmup_fn = optax.linear_schedule(init_value=0.0,
        end_value=base_learning_rate,
        transition_steps=config.warmup_steps)
    cosine_steps = max(config.num_train_steps - config.warmup_steps, 1)
    cosine_fn = optax.cosine_decay_schedule(init_value=base_learning_rate,
        decay_steps=cosine_steps)
    schedule_fn = optax.join_schedules(schedules=[warmup_fn, cosine_fn],
        boundaries=[config.warmup_steps])
    return schedule_fn


def train_step(state, batch, learning_rate_schedule):
    """Perform a single training step."""

    def loss_fn(params):
        """loss function used for training."""
        outputs, new_model_state = state.apply_fn(
            {'params': params, 'batch_stats': state.batch_stats},
            batch['image'],
            mutable=['batch_stats'])
        loss = gaussian_loss(outputs, batch['truth'])
        # weight_penalty_params = jax.tree_util.tree_leaves(params)
        # weight_decay = 0.0001
        # weight_l2 = sum(jnp.sum(x ** 2)
        #                 for x in weight_penalty_params
        #                 if x.ndim > 1)
        # weight_penalty = weight_decay * 0.5 * weight_l2
        # loss = loss + weight_penalty
        return loss, (new_model_state, outputs)

    step = state.step
    lr = learning_rate_schedule(step)

    grad_fn = jax.value_and_grad(loss_fn, has_aux=True)
    aux, grads = grad_fn(state.params)
    # Re-use same axis_name as in the call to `pmap(...train_step...)` below.
    grads = lax.pmean(grads, axis_name='batch')
    new_model_state, outputs = aux[1]
    metrics = compute_metrics(outputs, batch['truth'])
    metrics['learning_rate'] = lr

    new_state = state.apply_gradients(
        grads=grads, batch_stats=new_model_state['batch_stats'])

    return new_state, metrics


class TrainState(train_state.TrainState):
    batch_stats: Any


def restore_checkpoint(state, workdir):
    return checkpoints.restore_checkpoint(workdir, state)


def save_checkpoint(state, workdir, keep_every_n_steps=None):
    if jax.process_index() == 0:
        # get train state from the first replica
        state = jax.device_get(jax.tree_util.tree_map(lambda x: x[0], state))
        step = int(state.step)
        checkpoints.save_checkpoint(workdir, state, step, keep=3,
            keep_every_n_steps=keep_every_n_steps)


# pmean only works inside pmap because it needs an axis name.
# This function will average the inputs across all devices.
cross_replica_mean = jax.pmap(lambda x: lax.pmean(x, 'x'), 'x')


def sync_batch_stats(state):
    """Sync the batch statistics across replicas."""
    # Each device has its own version of the running average batch statistics and
    # we sync them before evaluation.
    return state.replace(batch_stats=cross_replica_mean(state.batch_stats))


def create_train_state(rng, config: ml_collections.ConfigDict,
                       model, image_size, learning_rate_schedule):
    """Create initial training state."""
    params, batch_stats = initialized(rng, image_size, model)
    tx = optax.adam(
        learning_rate=learning_rate_schedule
    )
    state = TrainState.create(
        apply_fn=model.apply,
        params=params,
        tx=tx,
        batch_stats=batch_stats)
    return state


def train_and_evaluate(config: ml_collections.ConfigDict,
                       input_config: dict,
                       workdir: str,
                       rng: Union[Iterator[Sequence[int]], Sequence[int]],
                       image_size: int, learning_rate: float) -> TrainState:
    """
    """

    writer = metric_writers.create_default_writer(
        logdir=workdir, just_logging=jax.process_index() != 0)

    if config.batch_size % jax.device_count() > 0:
        raise ValueError('Batch size must be divisible by the number of devices')

    steps_per_epoch = config.steps_per_epoch
    num_steps = config.num_train_steps

    base_learning_rate = learning_rate * config.batch_size / 256.

    model_cls = getattr(models, config.model)
    num_outputs = len(input_config['truth_parameters'][0]) * 2
    model = model_cls(num_outputs=num_outputs, dtype=jnp.float32)

    learning_rate_schedule = get_learning_rate_schedule(config,
        base_learning_rate)

    if isinstance(rng, Iterator):
        rng_state = next(rng)
    else:
        rng, rng_state = jax.random.split(rng)
    state = create_train_state(rng_state, config, model, image_size,
        learning_rate_schedule)
    state = restore_checkpoint(state, workdir)

    # step_offset > 0 if restarting from checkpoint
    step_offset = int(state.step)
    state = jax_utils.replicate(state)

    p_train_step = jax.pmap(functools.partial(train_step,
        learning_rate_schedule=learning_rate_schedule),
        axis_name='batch')
    draw_image_and_truth_pmap = jax.pmap(jax.jit(jax.vmap(
        functools.partial(
            input_pipeline.draw_image_and_truth,
            all_models=input_config['all_models'],
            principal_md_index=input_config['principal_md_index'],
            principal_source_index=input_config['principal_source_index'],
            kwargs_simulation=input_config['kwargs_simulation'],
            kwargs_detector=input_config['kwargs_detector'],
            kwargs_psf=input_config['kwargs_psf'],
            truth_parameters=input_config['truth_parameters']),
        in_axes=(None, None, None, None, 0))),
        in_axes=(None, None, None, None, 0)
    )
    if isinstance(rng, Iterator):
        rng_cosmo = next(rng)
    else:
        rng, rng_cosmo = jax.random.split(rng)
    cosmology_params = input_pipeline.intialize_cosmology_params(input_config,
                                                                 rng_cosmo)
    grid_x, grid_y = input_pipeline.generate_grids(input_config)

    train_metrics = []
    hooks = []
    if jax.process_index() == 0:
        hooks += [periodic_actions.Profile(num_profile_steps=5, logdir=workdir)]
    train_metrics_last_t = time.time()
    print('Initial compilation, this might take some minutes...')
    for step in range(step_offset, num_steps):

        # Generate truths and images
        if isinstance(rng, Iterator):
            rng_images = next(rng)
        else:
            rng, rng_images = jax.random.split(rng)
        rng_images = jax.random.split(
            rng_images, num=jax.device_count() * config.batch_size).reshape(
                (jax.device_count(), config.batch_size, -1)
            )

        image, truth = draw_image_and_truth_pmap(input_config['lensing_config'],
                                                 cosmology_params, grid_x,
                                                 grid_y, rng_images)
        image = jnp.expand_dims(image, axis=-1)

        batch = {'image': image, 'truth': truth}
        state, metrics = p_train_step(state, batch)
        for h in hooks:
            h(step)
        if step == step_offset:
            print('Initial compilation completed.')

        train_metrics.append(metrics)
        if (step + 1) % steps_per_epoch == 0:
            train_metrics = common_utils.get_metrics(train_metrics)
            summary = {
                f'train_{k}': v
                for k, v in jax.tree_util.tree_map(lambda x: x.mean(), train_metrics).items()
            }
            summary['steps_per_second'] = steps_per_epoch / (
                time.time() - train_metrics_last_t)
            writer.write_scalars(step + 1, summary)
            print(summary)
            train_metrics = []
            train_metrics_last_t = time.time()

        if (step + 1) % steps_per_epoch == 0 or step + 1 == num_steps:
            state = sync_batch_stats(state)
            save_checkpoint(state, workdir, config.keep_every_n_steps)

    # Wait until computations are done before exiting
    jax.random.normal(jax.random.PRNGKey(0), ()).block_until_ready()

    return state


def main(_):
    from jaxstronomy import train_config
    from jaxstronomy import input_config

    config = train_config.get_config()
    ic = input_config.get_config()
    image_size = ic['kwargs_detector']['n_x']
    rng = jax.random.PRNGKey(0)
    if FLAGS.num_unique_batches > 0:
        rng_list = jax.random.split(rng, FLAGS.num_unique_batches)
        rng = itertools.cycle(rng_list)
    train_and_evaluate(config, ic, FLAGS.workdir, rng, image_size,
                       FLAGS.learning_rate)


if __name__ == '__main__':
    app.run(main)