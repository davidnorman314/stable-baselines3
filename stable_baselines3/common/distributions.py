"""Probability distributions."""

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Tuple, Union

import gym
import numpy as np
import torch as th
from gym import spaces
from torch import nn
from torch.distributions import Bernoulli, Categorical, Normal

from stable_baselines3.common.preprocessing import get_action_dim

import traceback
import sys


class Distribution(ABC):
    """Abstract base class for distributions."""

    def __init__(self):
        super().__init__()
        self.distribution = None

    @abstractmethod
    def proba_distribution_net(
        self, *args, **kwargs
    ) -> Union[nn.Module, Tuple[nn.Module, nn.Parameter]]:
        """Create the layers and parameters that represent the distribution.

        Subclasses must define this, but the arguments and return type vary between
        concrete classes."""

    @abstractmethod
    def proba_distribution(self, *args, **kwargs) -> "Distribution":
        """Set parameters of the distribution.

        :return: self
        """

    @abstractmethod
    def log_prob(self, x: th.Tensor) -> th.Tensor:
        """
        Returns the log likelihood

        :param x: the taken action
        :return: The log likelihood of the distribution
        """

    @abstractmethod
    def entropy(self) -> Optional[th.Tensor]:
        """
        Returns Shannon's entropy of the probability

        :return: the entropy, or None if no analytical form is known
        """

    @abstractmethod
    def sample(self) -> th.Tensor:
        """
        Returns a sample from the probability distribution

        :return: the stochastic action
        """

    @abstractmethod
    def mode(self) -> th.Tensor:
        """
        Returns the most likely action (deterministic output)
        from the probability distribution

        :return: the stochastic action
        """

    def get_actions(self, deterministic: bool = False) -> th.Tensor:
        """
        Return actions according to the probability distribution.

        :param deterministic:
        :return:
        """
        if deterministic:
            return self.mode()
        return self.sample()

    @abstractmethod
    def actions_from_params(self, *args, **kwargs) -> th.Tensor:
        """
        Returns samples from the probability distribution
        given its parameters.

        :return: actions
        """

    @abstractmethod
    def log_prob_from_params(self, *args, **kwargs) -> Tuple[th.Tensor, th.Tensor]:
        """
        Returns samples and the associated log probabilities
        from the probability distribution given its parameters.

        :return: actions and log prob
        """


def sum_independent_dims(tensor: th.Tensor) -> th.Tensor:
    """
    Continuous actions are usually considered to be independent,
    so we can sum components of the ``log_prob`` or the entropy.

    :param tensor: shape: (n_batch, n_actions) or (n_batch,)
    :return: shape: (n_batch,)
    """
    if len(tensor.shape) > 1:
        tensor = tensor.sum(dim=1)
    else:
        tensor = tensor.sum()
    return tensor


class DiagGaussianDistribution(Distribution):
    """
    Gaussian distribution with diagonal covariance matrix, for continuous actions.

    :param action_dim:  Dimension of the action space.
    """

    def __init__(self, action_dim: int):
        super().__init__()
        self.action_dim = action_dim
        self.mean_actions = None
        self.log_std = None

    def proba_distribution_net(
        self, latent_dim: int, log_std_init: float = 0.0
    ) -> Tuple[nn.Module, nn.Parameter]:
        """
        Create the layers and parameter that represent the distribution:
        one output will be the mean of the Gaussian, the other parameter will be the
        standard deviation (log std in fact to allow negative values)

        :param latent_dim: Dimension of the last layer of the policy (before the action layer)
        :param log_std_init: Initial value for the log standard deviation
        :return:
        """
        mean_actions = nn.Linear(latent_dim, self.action_dim)
        # TODO: allow action dependent std
        log_std = nn.Parameter(
            th.ones(self.action_dim) * log_std_init, requires_grad=True
        )
        return mean_actions, log_std

    def proba_distribution(
        self, mean_actions: th.Tensor, log_std: th.Tensor
    ) -> "DiagGaussianDistribution":
        """
        Create the distribution given its parameters (mean, std)

        :param mean_actions:
        :param log_std:
        :return:
        """
        action_std = th.ones_like(mean_actions) * log_std.exp()
        self.distribution = Normal(mean_actions, action_std)
        return self

    def log_prob(self, actions: th.Tensor) -> th.Tensor:
        """
        Get the log probabilities of actions according to the distribution.
        Note that you must first call the ``proba_distribution()`` method.

        :param actions:
        :return:
        """
        log_prob = self.distribution.log_prob(actions)
        return sum_independent_dims(log_prob)

    def entropy(self) -> th.Tensor:
        return sum_independent_dims(self.distribution.entropy())

    def sample(self) -> th.Tensor:
        # Reparametrization trick to pass gradients
        return self.distribution.rsample()

    def mode(self) -> th.Tensor:
        return self.distribution.mean

    def actions_from_params(
        self, mean_actions: th.Tensor, log_std: th.Tensor, deterministic: bool = False
    ) -> th.Tensor:
        # Update the proba distribution
        self.proba_distribution(mean_actions, log_std)
        return self.get_actions(deterministic=deterministic)

    def log_prob_from_params(
        self, mean_actions: th.Tensor, log_std: th.Tensor
    ) -> Tuple[th.Tensor, th.Tensor]:
        """
        Compute the log probability of taking an action
        given the distribution parameters.

        :param mean_actions:
        :param log_std:
        :return:
        """
        actions = self.actions_from_params(mean_actions, log_std)
        log_prob = self.log_prob(actions)
        return actions, log_prob


class SquashedDiagGaussianDistribution(DiagGaussianDistribution):
    """
    Gaussian distribution with diagonal covariance matrix, followed by a squashing function (tanh) to ensure bounds.

    :param action_dim: Dimension of the action space.
    :param epsilon: small value to avoid NaN due to numerical imprecision.
    """

    def __init__(self, action_dim: int, epsilon: float = 1e-6):
        super().__init__(action_dim)
        # Avoid NaN (prevents division by zero or log of zero)
        self.epsilon = epsilon
        self.gaussian_actions = None

    def proba_distribution(
        self, mean_actions: th.Tensor, log_std: th.Tensor
    ) -> "SquashedDiagGaussianDistribution":
        super().proba_distribution(mean_actions, log_std)
        return self

    def log_prob(
        self, actions: th.Tensor, gaussian_actions: Optional[th.Tensor] = None
    ) -> th.Tensor:
        # Inverse tanh
        # Naive implementation (not stable): 0.5 * torch.log((1 + x) / (1 - x))
        # We use numpy to avoid numerical instability
        if gaussian_actions is None:
            # It will be clipped to avoid NaN when inversing tanh
            gaussian_actions = TanhBijector.inverse(actions)

        # Log likelihood for a Gaussian distribution
        log_prob = super().log_prob(gaussian_actions)
        # Squash correction (from original SAC implementation)
        # this comes from the fact that tanh is bijective and differentiable
        log_prob -= th.sum(th.log(1 - actions ** 2 + self.epsilon), dim=1)
        return log_prob

    def entropy(self) -> Optional[th.Tensor]:
        # No analytical form,
        # entropy needs to be estimated using -log_prob.mean()
        return None

    def sample(self) -> th.Tensor:
        # Reparametrization trick to pass gradients
        self.gaussian_actions = super().sample()
        return th.tanh(self.gaussian_actions)

    def mode(self) -> th.Tensor:
        self.gaussian_actions = super().mode()
        # Squash the output
        return th.tanh(self.gaussian_actions)

    def log_prob_from_params(
        self, mean_actions: th.Tensor, log_std: th.Tensor
    ) -> Tuple[th.Tensor, th.Tensor]:
        action = self.actions_from_params(mean_actions, log_std)
        log_prob = self.log_prob(action, self.gaussian_actions)
        return action, log_prob


class CategoricalDistribution(Distribution):
    """
    Categorical distribution for discrete actions.

    :param action_dim: Number of discrete actions
    """

    def __init__(self, action_dim: int):
        super().__init__()
        self.action_dim = action_dim
        print("In CategoricalDistribution constructor")
        raise Exception("In CategoricalDistribution")

    def proba_distribution_net(self, latent_dim: int) -> nn.Module:
        """
        Create the layer that represents the distribution:
        it will be the logits of the Categorical distribution.
        You can then get probabilities using a softmax.

        :param latent_dim: Dimension of the last layer
            of the policy network (before the action layer)
        :return:
        """
        action_logits = nn.Linear(latent_dim, self.action_dim)
        return action_logits

    def proba_distribution(self, action_logits: th.Tensor) -> "CategoricalDistribution":
        self.distribution = Categorical(logits=action_logits)
        return self

    def log_prob(self, actions: th.Tensor) -> th.Tensor:
        return self.distribution.log_prob(actions)

    def entropy(self) -> th.Tensor:
        return self.distribution.entropy()

    def sample(self) -> th.Tensor:
        return self.distribution.sample()

    def mode(self) -> th.Tensor:
        return th.argmax(self.distribution.probs, dim=1)

    def actions_from_params(
        self, action_logits: th.Tensor, deterministic: bool = False
    ) -> th.Tensor:
        # Update the proba distribution
        self.proba_distribution(action_logits)
        return self.get_actions(deterministic=deterministic)

    def log_prob_from_params(
        self, action_logits: th.Tensor
    ) -> Tuple[th.Tensor, th.Tensor]:
        actions = self.actions_from_params(action_logits)
        log_prob = self.log_prob(actions)
        return actions, log_prob


class CategoricalDistributionLimitedActions(Distribution):
    """
    Categorical distribution for discrete actions where the valid actions are a subset of the total action space.

    :param action_dim: Number of discrete actions
    """

    def __init__(self, action_dim: int, get_invalid_actions_layer_obj):
        super().__init__()
        self.action_dim = action_dim

        if get_invalid_actions_layer_obj is None:
            raise Exception("get_invalid_actions_layer is none")

        self._get_invalid_actions_layer = get_invalid_actions_layer_obj()
        self._get_invalid_actions_obj = get_invalid_actions_layer_obj
        self._original_action_logits = None
        self._valid_actions = None
        self._invalid_actions = None

        print("In CategoricalDistributionLimitedActions constructor")

    def proba_distribution_net(self, latent_dim: int) -> nn.Module:
        """
        Create the layer that represents the distribution:
        it will be the logits of the Categorical distribution.
        You can then get probabilities using a softmax.

        :param latent_dim: Dimension of the last layer
            of the policy network (before the action layer)
        :return:
        """
        action_logits = nn.Linear(latent_dim, self.action_dim)
        return action_logits

    def proba_distribution(
        self,
        action_logits: th.Tensor,
        obs: th.Tensor,
    ) -> "CategoricalDistributionLimitedActions":
        if obs is None:
            raise Exception("In get_valid_actions the observation is None")

        invalid_actions_float = self._get_invalid_actions_layer(obs)
        invalid_actions = invalid_actions_float > 0.0

        self._original_action_logits = action_logits
        self._invalid_actions = invalid_actions
        if False:
            self._valid_actions = self._get_invalid_actions_obj.get_valid_actions(obs)

        # TODO: Figure out how to set the invalid action minimum on a per-action basis.
        if False:
            print("")
            print("----------------------------")
            print("")
            print("action_logits", action_logits)
            print("action_logits len", len(action_logits))
            mint = action_logits.min(1, keepdim=True)
            print("mint true", mint)
            print("mint true len", len(mint.values))
            # mint = action_logits.min(1, keepdim=False)
            # print("mint false", mint)

            print("invalid", invalid_actions)
            test_new_action_logits = action_logits.clone()
            test_new_action_logits[invalid_actions] = mint.values
            print(test_new_action_logits)

        # Create a distribution to use when sampling.
        # Adjust the logits so that the invalid actions have small probabilities.
        invalid_logit_value = action_logits.min() - 10.0
        if invalid_logit_value >= -10.0:
            invalid_logit_value = -10.0

        new_action_logits = action_logits.clone()
        new_action_logits[invalid_actions] = invalid_logit_value

        self._invalid_logit_value = invalid_logit_value
        self._new_action_logits = new_action_logits

        if False:
            print("")
            print("----------------------------")
            print("")
            print("action_logits", action_logits)
            print("new_action_logits", new_action_logits)
            print("invalid_actions", invalid_actions)
            print("obs", obs)
            print("invalid_logit_value", invalid_logit_value)
            # traceback.print_stack(file=sys.stdout)

        self.distribution = Categorical(logits=new_action_logits)
        return self

    def log_prob(self, actions: th.Tensor) -> th.Tensor:
        return self.distribution.log_prob(actions)

    def entropy(self) -> th.Tensor:
        return self.distribution.entropy()

    def sample(self) -> th.Tensor:
        ret = self.distribution.sample()

        if False:
            print(self.distribution)
            print(self.distribution.logits)
            print(self.sample_distribution)
            print(self.sample_distribution.logits)
            print("Sample", ret)

        if False and self.distribution.logits[0][ret] < -9.5:
            print("Invalid action")
            print(self.distribution)
            print(self.distribution.logits)
            print("Invalid_logit_value", self._invalid_logit_value)
            print("original_action_logits", self._original_action_logits)
            print("new_action_logits", self._new_action_logits)
            print("invalid_actions", self._invalid_actions)
            print("valid_actions", self._valid_actions)
            print("Sample", ret)

        return ret

    def mode(self) -> th.Tensor:
        return th.argmax(self.distribution.probs, dim=1)

    def actions_from_params(
        self, action_logits: th.Tensor, get_valid_actions, deterministic: bool = False
    ) -> th.Tensor:
        # Update the proba distribution
        self.proba_distribution(action_logits, get_valid_actions)
        return self.get_actions(deterministic=deterministic)

    def log_prob_from_params(
        self, action_logits: th.Tensor, get_valid_actions
    ) -> Tuple[th.Tensor, th.Tensor]:
        actions = self.actions_from_params(action_logits, get_valid_actions)
        log_prob = self.log_prob(actions)
        return actions, log_prob


class CategoricalDistributionLimitedActions_Manual(Distribution):
    """
    Categorical distribution for discrete actions where the valid actions are a subset of the total action space.

    :param action_dim: Number of discrete actions
    """

    def __init__(self, action_dim: int, get_valid_actions):
        super().__init__()
        self.action_dim = action_dim
        self._get_valid_actions = get_valid_actions

        if get_valid_actions is None:
            raise Exception("get_valid_actions is none")

        print("In CategoricalDistributionLimitedActions constructor")

    def proba_distribution_net(self, latent_dim: int) -> nn.Module:
        """
        Create the layer that represents the distribution:
        it will be the logits of the Categorical distribution.
        You can then get probabilities using a softmax.

        :param latent_dim: Dimension of the last layer
            of the policy network (before the action layer)
        :return:
        """
        action_logits = nn.Linear(latent_dim, self.action_dim)
        return action_logits

    def proba_distribution(
        self,
        action_logits: th.Tensor,
        obs: th.Tensor,
    ) -> "CategoricalDistributionLimitedActions":
        if obs is None:
            raise Exception("In get_valid_actions the observation is None")

        self.distribution = Categorical(logits=action_logits)

        # Create a distribution to use when sampling.
        # Adjust the logits so that the invalid actions have small probabilities.
        invalid_logit_value = action_logits.min() - 100.0
        if invalid_logit_value >= -100.0:
            invalid_logit_value = -100.0

        valid_actions_list = []
        for obs_index in range(len(obs)):
            valid_actions = self._get_valid_actions(obs[obs_index])
            valid_actions_list.append(valid_actions)

        all_valid_actions = th.stack(valid_actions_list)

        new_action_logits = th.where(
            all_valid_actions == 0, invalid_logit_value, action_logits
        )

        if True:
            print("")
            print("----------------------------")
            print("")
            print("action_logits", action_logits)
            print("new_action_logits", new_action_logits)
            print("obs", obs)
            print("all_valid_actions", all_valid_actions)
            print("invalid_logit_value", invalid_logit_value)
            # traceback.print_stack(file=sys.stdout)

        self.sample_distribution = Categorical(logits=new_action_logits)
        return self

    def log_prob(self, actions: th.Tensor) -> th.Tensor:
        return self.distribution.log_prob(actions)

    def entropy(self) -> th.Tensor:
        return self.distribution.entropy()

    def sample(self) -> th.Tensor:
        ret = self.sample_distribution.sample()

        if False:
            print(self.distribution)
            print(self.distribution.logits)
            print(self.sample_distribution)
            print(self.sample_distribution.logits)
            print("Sample", ret)

        if True and self.sample_distribution.logits[0][ret] < -9.5:
            print("Invalid action")
            print(self.distribution)
            print(self.distribution.logits)
            print(self.sample_distribution)
            print(self.sample_distribution.logits)
            print("Sample", ret)

        return ret

    def mode(self) -> th.Tensor:
        return th.argmax(self.sample_distribution.probs, dim=1)

    def actions_from_params(
        self, action_logits: th.Tensor, get_valid_actions, deterministic: bool = False
    ) -> th.Tensor:
        # Update the proba distribution
        self.proba_distribution(action_logits, get_valid_actions)
        return self.get_actions(deterministic=deterministic)

    def log_prob_from_params(
        self, action_logits: th.Tensor, get_valid_actions
    ) -> Tuple[th.Tensor, th.Tensor]:
        actions = self.actions_from_params(action_logits, get_valid_actions)
        log_prob = self.log_prob(actions)
        return actions, log_prob


class MultiCategoricalDistribution(Distribution):
    """
    MultiCategorical distribution for multi discrete actions.

    :param action_dims: List of sizes of discrete action spaces
    """

    def __init__(self, action_dims: List[int]):
        super().__init__()
        self.action_dims = action_dims

    def proba_distribution_net(self, latent_dim: int) -> nn.Module:
        """
        Create the layer that represents the distribution:
        it will be the logits (flattened) of the MultiCategorical distribution.
        You can then get probabilities using a softmax on each sub-space.

        :param latent_dim: Dimension of the last layer
            of the policy network (before the action layer)
        :return:
        """

        action_logits = nn.Linear(latent_dim, sum(self.action_dims))
        return action_logits

    def proba_distribution(
        self, action_logits: th.Tensor
    ) -> "MultiCategoricalDistribution":
        self.distribution = [
            Categorical(logits=split)
            for split in th.split(action_logits, tuple(self.action_dims), dim=1)
        ]
        return self

    def log_prob(self, actions: th.Tensor) -> th.Tensor:
        # Extract each discrete action and compute log prob for their respective distributions
        return th.stack(
            [
                dist.log_prob(action)
                for dist, action in zip(self.distribution, th.unbind(actions, dim=1))
            ],
            dim=1,
        ).sum(dim=1)

    def entropy(self) -> th.Tensor:
        return th.stack([dist.entropy() for dist in self.distribution], dim=1).sum(
            dim=1
        )

    def sample(self) -> th.Tensor:
        return th.stack([dist.sample() for dist in self.distribution], dim=1)

    def mode(self) -> th.Tensor:
        return th.stack(
            [th.argmax(dist.probs, dim=1) for dist in self.distribution], dim=1
        )

    def actions_from_params(
        self, action_logits: th.Tensor, deterministic: bool = False
    ) -> th.Tensor:
        # Update the proba distribution
        self.proba_distribution(action_logits)
        return self.get_actions(deterministic=deterministic)

    def log_prob_from_params(
        self, action_logits: th.Tensor
    ) -> Tuple[th.Tensor, th.Tensor]:
        actions = self.actions_from_params(action_logits)
        log_prob = self.log_prob(actions)
        return actions, log_prob


class BernoulliDistribution(Distribution):
    """
    Bernoulli distribution for MultiBinary action spaces.

    :param action_dim: Number of binary actions
    """

    def __init__(self, action_dims: int):
        super().__init__()
        self.action_dims = action_dims

    def proba_distribution_net(self, latent_dim: int) -> nn.Module:
        """
        Create the layer that represents the distribution:
        it will be the logits of the Bernoulli distribution.

        :param latent_dim: Dimension of the last layer
            of the policy network (before the action layer)
        :return:
        """
        action_logits = nn.Linear(latent_dim, self.action_dims)
        return action_logits

    def proba_distribution(self, action_logits: th.Tensor) -> "BernoulliDistribution":
        self.distribution = Bernoulli(logits=action_logits)
        return self

    def log_prob(self, actions: th.Tensor) -> th.Tensor:
        return self.distribution.log_prob(actions).sum(dim=1)

    def entropy(self) -> th.Tensor:
        return self.distribution.entropy().sum(dim=1)

    def sample(self) -> th.Tensor:
        return self.distribution.sample()

    def mode(self) -> th.Tensor:
        return th.round(self.distribution.probs)

    def actions_from_params(
        self, action_logits: th.Tensor, deterministic: bool = False
    ) -> th.Tensor:
        # Update the proba distribution
        self.proba_distribution(action_logits)
        return self.get_actions(deterministic=deterministic)

    def log_prob_from_params(
        self, action_logits: th.Tensor
    ) -> Tuple[th.Tensor, th.Tensor]:
        actions = self.actions_from_params(action_logits)
        log_prob = self.log_prob(actions)
        return actions, log_prob


class StateDependentNoiseDistribution(Distribution):
    """
    Distribution class for using generalized State Dependent Exploration (gSDE).
    Paper: https://arxiv.org/abs/2005.05719

    It is used to create the noise exploration matrix and
    compute the log probability of an action with that noise.

    :param action_dim: Dimension of the action space.
    :param full_std: Whether to use (n_features x n_actions) parameters
        for the std instead of only (n_features,)
    :param use_expln: Use ``expln()`` function instead of ``exp()`` to ensure
        a positive standard deviation (cf paper). It allows to keep variance
        above zero and prevent it from growing too fast. In practice, ``exp()`` is usually enough.
    :param squash_output: Whether to squash the output using a tanh function,
        this ensures bounds are satisfied.
    :param learn_features: Whether to learn features for gSDE or not.
        This will enable gradients to be backpropagated through the features
        ``latent_sde`` in the code.
    :param epsilon: small value to avoid NaN due to numerical imprecision.
    """

    def __init__(
        self,
        action_dim: int,
        full_std: bool = True,
        use_expln: bool = False,
        squash_output: bool = False,
        learn_features: bool = False,
        epsilon: float = 1e-6,
    ):
        super().__init__()
        self.action_dim = action_dim
        self.latent_sde_dim = None
        self.mean_actions = None
        self.log_std = None
        self.weights_dist = None
        self.exploration_mat = None
        self.exploration_matrices = None
        self._latent_sde = None
        self.use_expln = use_expln
        self.full_std = full_std
        self.epsilon = epsilon
        self.learn_features = learn_features
        if squash_output:
            self.bijector = TanhBijector(epsilon)
        else:
            self.bijector = None

    def get_std(self, log_std: th.Tensor) -> th.Tensor:
        """
        Get the standard deviation from the learned parameter
        (log of it by default). This ensures that the std is positive.

        :param log_std:
        :return:
        """
        if self.use_expln:
            # From gSDE paper, it allows to keep variance
            # above zero and prevent it from growing too fast
            below_threshold = th.exp(log_std) * (log_std <= 0)
            # Avoid NaN: zeros values that are below zero
            safe_log_std = log_std * (log_std > 0) + self.epsilon
            above_threshold = (th.log1p(safe_log_std) + 1.0) * (log_std > 0)
            std = below_threshold + above_threshold
        else:
            # Use normal exponential
            std = th.exp(log_std)

        if self.full_std:
            return std
        # Reduce the number of parameters:
        return th.ones(self.latent_sde_dim, self.action_dim).to(log_std.device) * std

    def sample_weights(self, log_std: th.Tensor, batch_size: int = 1) -> None:
        """
        Sample weights for the noise exploration matrix,
        using a centered Gaussian distribution.

        :param log_std:
        :param batch_size:
        """
        std = self.get_std(log_std)
        self.weights_dist = Normal(th.zeros_like(std), std)
        # Reparametrization trick to pass gradients
        self.exploration_mat = self.weights_dist.rsample()
        # Pre-compute matrices in case of parallel exploration
        self.exploration_matrices = self.weights_dist.rsample((batch_size,))

    def proba_distribution_net(
        self,
        latent_dim: int,
        log_std_init: float = -2.0,
        latent_sde_dim: Optional[int] = None,
    ) -> Tuple[nn.Module, nn.Parameter]:
        """
        Create the layers and parameter that represent the distribution:
        one output will be the deterministic action, the other parameter will be the
        standard deviation of the distribution that control the weights of the noise matrix.

        :param latent_dim: Dimension of the last layer of the policy (before the action layer)
        :param log_std_init: Initial value for the log standard deviation
        :param latent_sde_dim: Dimension of the last layer of the features extractor
            for gSDE. By default, it is shared with the policy network.
        :return:
        """
        # Network for the deterministic action, it represents the mean of the distribution
        mean_actions_net = nn.Linear(latent_dim, self.action_dim)
        # When we learn features for the noise, the feature dimension
        # can be different between the policy and the noise network
        self.latent_sde_dim = latent_dim if latent_sde_dim is None else latent_sde_dim
        # Reduce the number of parameters if needed
        log_std = (
            th.ones(self.latent_sde_dim, self.action_dim)
            if self.full_std
            else th.ones(self.latent_sde_dim, 1)
        )
        # Transform it to a parameter so it can be optimized
        log_std = nn.Parameter(log_std * log_std_init, requires_grad=True)
        # Sample an exploration matrix
        self.sample_weights(log_std)
        return mean_actions_net, log_std

    def proba_distribution(
        self, mean_actions: th.Tensor, log_std: th.Tensor, latent_sde: th.Tensor
    ) -> "StateDependentNoiseDistribution":
        """
        Create the distribution given its parameters (mean, std)

        :param mean_actions:
        :param log_std:
        :param latent_sde:
        :return:
        """
        # Stop gradient if we don't want to influence the features
        self._latent_sde = latent_sde if self.learn_features else latent_sde.detach()
        variance = th.mm(self._latent_sde ** 2, self.get_std(log_std) ** 2)
        self.distribution = Normal(mean_actions, th.sqrt(variance + self.epsilon))
        return self

    def log_prob(self, actions: th.Tensor) -> th.Tensor:
        if self.bijector is not None:
            gaussian_actions = self.bijector.inverse(actions)
        else:
            gaussian_actions = actions
        # log likelihood for a gaussian
        log_prob = self.distribution.log_prob(gaussian_actions)
        # Sum along action dim
        log_prob = sum_independent_dims(log_prob)

        if self.bijector is not None:
            # Squash correction (from original SAC implementation)
            log_prob -= th.sum(
                self.bijector.log_prob_correction(gaussian_actions), dim=1
            )
        return log_prob

    def entropy(self) -> Optional[th.Tensor]:
        if self.bijector is not None:
            # No analytical form,
            # entropy needs to be estimated using -log_prob.mean()
            return None
        return sum_independent_dims(self.distribution.entropy())

    def sample(self) -> th.Tensor:
        noise = self.get_noise(self._latent_sde)
        actions = self.distribution.mean + noise
        if self.bijector is not None:
            return self.bijector.forward(actions)
        return actions

    def mode(self) -> th.Tensor:
        actions = self.distribution.mean
        if self.bijector is not None:
            return self.bijector.forward(actions)
        return actions

    def get_noise(self, latent_sde: th.Tensor) -> th.Tensor:
        latent_sde = latent_sde if self.learn_features else latent_sde.detach()
        # Default case: only one exploration matrix
        if len(latent_sde) == 1 or len(latent_sde) != len(self.exploration_matrices):
            return th.mm(latent_sde, self.exploration_mat)
        # Use batch matrix multiplication for efficient computation
        # (batch_size, n_features) -> (batch_size, 1, n_features)
        latent_sde = latent_sde.unsqueeze(dim=1)
        # (batch_size, 1, n_actions)
        noise = th.bmm(latent_sde, self.exploration_matrices)
        return noise.squeeze(dim=1)

    def actions_from_params(
        self,
        mean_actions: th.Tensor,
        log_std: th.Tensor,
        latent_sde: th.Tensor,
        deterministic: bool = False,
    ) -> th.Tensor:
        # Update the proba distribution
        self.proba_distribution(mean_actions, log_std, latent_sde)
        return self.get_actions(deterministic=deterministic)

    def log_prob_from_params(
        self, mean_actions: th.Tensor, log_std: th.Tensor, latent_sde: th.Tensor
    ) -> Tuple[th.Tensor, th.Tensor]:
        actions = self.actions_from_params(mean_actions, log_std, latent_sde)
        log_prob = self.log_prob(actions)
        return actions, log_prob


class TanhBijector:
    """
    Bijective transformation of a probability distribution
    using a squashing function (tanh)
    TODO: use Pyro instead (https://pyro.ai/)

    :param epsilon: small value to avoid NaN due to numerical imprecision.
    """

    def __init__(self, epsilon: float = 1e-6):
        super().__init__()
        self.epsilon = epsilon

    @staticmethod
    def forward(x: th.Tensor) -> th.Tensor:
        return th.tanh(x)

    @staticmethod
    def atanh(x: th.Tensor) -> th.Tensor:
        """
        Inverse of Tanh

        Taken from Pyro: https://github.com/pyro-ppl/pyro
        0.5 * torch.log((1 + x ) / (1 - x))
        """
        return 0.5 * (x.log1p() - (-x).log1p())

    @staticmethod
    def inverse(y: th.Tensor) -> th.Tensor:
        """
        Inverse tanh.

        :param y:
        :return:
        """
        eps = th.finfo(y.dtype).eps
        # Clip the action to avoid NaN
        return TanhBijector.atanh(y.clamp(min=-1.0 + eps, max=1.0 - eps))

    def log_prob_correction(self, x: th.Tensor) -> th.Tensor:
        # Squash correction (from original SAC implementation)
        return th.log(1.0 - th.tanh(x) ** 2 + self.epsilon)


def make_proba_distribution(
    action_space: gym.spaces.Space,
    use_sde: bool = False,
    dist_kwargs: Optional[Dict[str, Any]] = None,
) -> Distribution:
    """
    Return an instance of Distribution for the correct type of action space

    :param action_space: the input action space
    :param use_sde: Force the use of StateDependentNoiseDistribution
        instead of DiagGaussianDistribution
    :param dist_kwargs: Keyword arguments to pass to the probability distribution
    :return: the appropriate Distribution object
    """
    if dist_kwargs is None:
        dist_kwargs = {}

    use_limited_actions = dist_kwargs.get(
        "get_valid_actions"
    ) is not None or dist_kwargs.get("get_invalid_actions_layer")

    if isinstance(action_space, spaces.Box):
        cls = StateDependentNoiseDistribution if use_sde else DiagGaussianDistribution
        return cls(get_action_dim(action_space), **dist_kwargs)
    elif isinstance(action_space, spaces.Discrete) and use_limited_actions:
        get_invalid_actions_layer = dist_kwargs["get_invalid_actions_layer"]
        del dist_kwargs["get_invalid_actions_layer"]
        return CategoricalDistributionLimitedActions(
            action_space.n, get_invalid_actions_layer, **dist_kwargs
        )
    elif isinstance(action_space, spaces.Discrete) and use_limited_actions:
        get_valid_actions = dist_kwargs["get_valid_actions"]
        del dist_kwargs["get_valid_actions"]
        return CategoricalDistributionLimitedActions(
            action_space.n, get_valid_actions, **dist_kwargs
        )
    elif isinstance(action_space, spaces.Discrete):
        return CategoricalDistribution(action_space.n, **dist_kwargs)
    elif isinstance(action_space, spaces.MultiDiscrete):
        return MultiCategoricalDistribution(action_space.nvec, **dist_kwargs)
    elif isinstance(action_space, spaces.MultiBinary):
        return BernoulliDistribution(action_space.n, **dist_kwargs)
    else:
        raise NotImplementedError(
            "Error: probability distribution, not implemented for action space"
            f"of type {type(action_space)}."
            " Must be of type Gym Spaces: Box, Discrete, MultiDiscrete or MultiBinary."
        )


def kl_divergence(dist_true: Distribution, dist_pred: Distribution) -> th.Tensor:
    """
    Wrapper for the PyTorch implementation of the full form KL Divergence

    :param dist_true: the p distribution
    :param dist_pred: the q distribution
    :return: KL(dist_true||dist_pred)
    """
    # KL Divergence for different distribution types is out of scope
    assert (
        dist_true.__class__ == dist_pred.__class__
    ), "Error: input distributions should be the same type"

    # MultiCategoricalDistribution is not a PyTorch Distribution subclass
    # so we need to implement it ourselves!
    if isinstance(dist_pred, MultiCategoricalDistribution):
        assert np.allclose(
            dist_pred.action_dims, dist_true.action_dims
        ), "Error: distributions must have the same input space"
        return th.stack(
            [
                th.distributions.kl_divergence(p, q)
                for p, q in zip(dist_true.distribution, dist_pred.distribution)
            ],
            dim=1,
        ).sum(dim=1)

    # Use the PyTorch kl_divergence implementation
    else:
        return th.distributions.kl_divergence(
            dist_true.distribution, dist_pred.distribution
        )
