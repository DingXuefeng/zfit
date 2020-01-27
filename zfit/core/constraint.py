#  Copyright (c) 2020 zfit

import abc
from collections import OrderedDict
from typing import Dict, Union, Callable, Optional

import numpy as np
import tensorflow as tf
import tensorflow_probability as tfp
from ordered_set import OrderedSet

from zfit import z
from .baseobject import BaseNumeric
from .interfaces import ZfitConstraint
from .interfaces import ZfitParameter
from .parameter import convert_to_parameter
from ..settings import ztypes
from ..util import ztyping
from ..util.container import convert_to_container
from ..util.exception import ShapeIncompatibleError, LogicalUndefinedOperationError

tfd = tfp.distributions


class BaseConstraint(ZfitConstraint, BaseNumeric):

    def __init__(self, params: Union[Dict[str, ZfitParameter]] = None,
                 name: str = "BaseConstraint", dtype=ztypes.float,
                 **kwargs):
        """Base class for constraints.

        Args:
            dtype (DType): the dtype of the constraint
            name (str): the name of the constraint
            params (Dict(str, :py:class:`~zfit.Parameter`)): A dictionary with the internal name of the
                parameter and the parameters itself the constrains depends on
        """
        super().__init__(name=name, dtype=dtype, params=params, **kwargs)

    def value(self):
        return self._value()

    @abc.abstractmethod
    def _value(self):
        raise NotImplementedError

    def _get_dependents(self) -> ztyping.DependentsType:
        return self._extract_dependents(self.get_params())


class SimpleConstraint(BaseConstraint):

    def __init__(self, func: Callable, params: Optional[ztyping.ParametersType]):
        """Constraint from a (function returning a) Tensor.

        The parameters are named "param_{i}" with i starting from 0 and corresponding to the index of params.

        Args:
            func: Callable that constructs the constraint and returns a tensor.
            params: The dependents (independent `zfit.Parameter`) of the loss. If not given, the
                dependents are figured out automatically.
        """
        self._simple_func = func
        self._simple_func_dependents = convert_to_container(params, container=OrderedSet)

        params = convert_to_container(params, container=list)
        params = OrderedDict((f"param_{i}", p) for i, p in enumerate(params))

        super().__init__(name="SimpleConstraint", params=params)

    # def _get_dependents(self):
    #     dependents = self._simple_func_dependents
    #     if dependents is None:
    #         independent_params = tf.compat.v1.get_collection("zfit_independent")
    #         dependents = get_dependents_auto(tensor=self.value(), candidates=independent_params)
    #         self._simple_func_dependents = dependents
    #     return dependents

    def _value(self):
        return self._simple_func()


class SamplableConstraint(BaseConstraint):

    def __init__(self, x: Union[ztyping.NumericalScalarType, ZfitParameter],
                 params: Union[Dict[str, ZfitParameter]] = None, name: str = "SamplableConstraint",
                 dtype=ztypes.float, **kwargs):
        """Base class for samplable constraints.

        Args:
            dtype (DType): the dtype of the constraint
            name (str): the name of the constraint
            params (Dict(str, :py:class:`~zfit.Parameter`)): A dictionary with the internal name of the
                parameter and the parameters itself the constrains depends on
            x (numerical, list(numerical) or list(zfit.Parameter)): Observed values of the parameter
                to constraint obtained from auxiliary measurements.
        """
        super().__init__(name=name, dtype=dtype, params=params, **kwargs)
        x = convert_to_container(x, tuple)

        if len(x) != len(params):
            raise ShapeIncompatibleError("x and params have to be the same lenght. Currently"
                                         f"x: {len(x)}, params: {len(params)}")

        x = [convert_to_parameter(x_, f"{p.name}_obs") for x_, p in zip(x, params.values())]
        self._x = x

    @property
    def x(self):
        """
        Return the values of the constrained parameters obtained from auxiliary measurment.
        """
        return self._x

    @property
    def _x_array(self):
        return z.convert_to_tensor([z.convert_to_tensor(x) for x in self.x])

    def value(self):
        return self._value()

    @abc.abstractmethod
    def _value(self):
        raise NotImplementedError

    def _get_dependents(self) -> ztyping.DependentsType:
        return self._extract_dependents(self.get_params())

    def sample(self, n):
        """Sample `n` points from the probability density function for the observed value of the parameters.

        Args:
            n (int, tf.Tensor): The number of samples to be generated.
        Returns:
            Dict(Parameter: n_samples)
        """
        sample = self._sample(n=n)
        return {p: sample[:, i] for i, p in enumerate(self.x)}

    @abc.abstractmethod
    def _sample(self, n):
        raise NotImplementedError


class DistributionConstraint(SamplableConstraint):

    def __init__(self, x: Union[ztyping.NumericalScalarType, ZfitParameter],
                 params: Dict[str, ZfitParameter], distribution: tfd.Distribution,
                 dist_params, dist_kwargs=None, name: str = "DistributionConstraint", dtype=ztypes.float,
                 **kwargs):
        """ Base class for constraints using a probability density function.

        Args:
            distribution (`tensorflow_probability.distributions.Distribution`): The probability density function
                used to constraint the parameters

        """
        super().__init__(x=x, params=params, name=name, dtype=dtype, **kwargs)

        self._distribution = distribution
        self.dist_params = dist_params
        self.dist_kwargs = dist_kwargs if dist_kwargs is not None else {}

    @property
    def distribution(self):
        params = self.dist_params
        if callable(params):
            params = params()
        kwargs = self.dist_kwargs
        if callable(kwargs):
            kwargs = kwargs()
        return self._distribution(**params, **kwargs, name=self.name + "_tfp")

    def _value(self):
        value = -self.distribution.log_prob(self._x_array)
        return value

    def _sample(self, n):
        # TODO cache: add proper caching
        return self.distribution.sample(n)


class GaussianConstraint(DistributionConstraint):

    def __init__(self, x: ztyping.NumericalScalarType, mu: ztyping.ParamTypeInput,
                 sigma: ztyping.NumericalScalarType):
        """Gaussian constraints on a list of parameters.

        Args:
            x (numerical, list(numerical) or list(zfit.Parameter)): Observed values of the parameter
                to constraint obtained from auxiliary measurements
            mu (list(zfit.Parameter)): The parameters to constraint
            sigma (numerical, list(numerical) or array/tensor): The standard deviations or covariance
                matrix of the constraint. Can either be a single value, a list of values, an array or a tensor

        Raises:
            ShapeIncompatibleError: if params, mu and sigma don't have incompatible shapes
        """

        x = convert_to_container(x, tuple)
        mu = convert_to_container(mu, tuple)
        params_dict = {f"param_{i}": p for i, p in enumerate(mu)}

        def create_covariance(mu, sigma):
            mu = z.convert_to_tensor(mu)
            sigma = z.convert_to_tensor(sigma)  # TODO (Mayou36): fix as above?
            x_tensor = self._x_array

            if sigma.shape.ndims > 1:
                covariance = sigma
            elif sigma.shape.ndims == 1:
                covariance = tf.linalg.tensor_diag(z.pow(sigma, 2.))
            else:
                sigma = tf.reshape(sigma, [1])
                covariance = tf.linalg.tensor_diag(z.pow(sigma, 2.))

            if not x_tensor.shape[0] == mu.shape[0] == covariance.shape[0] == covariance.shape[1]:
                raise ShapeIncompatibleError(f"x, mu and sigma have to have the same length. Currently"
                                             f"x: {x_tensor.shape[0]}, mu: {mu.shape[0]}, "
                                             f"covariance (from sigma): {covariance.shape[0:2]}")
            return covariance

        self._mu = mu
        self._covariance = lambda: create_covariance(mu, sigma)
        distribution = tfd.MultivariateNormalFullCovariance
        dist_params = lambda: dict(loc=mu, covariance_matrix=self.covariance)
        dist_kwargs = dict(validate_args=True)

        super().__init__(name="GaussianConstraint", x=x, params=params_dict,
                         distribution=distribution, dist_params=dist_params, dist_kwargs=dist_kwargs)

    @property
    def mu(self):
        """
        Return the central values of the constraint.
        """
        return self._mu

    @property
    def covariance(self):
        """
        Return the covariance matrix of the constraint.
        """
        return self._covariance()
