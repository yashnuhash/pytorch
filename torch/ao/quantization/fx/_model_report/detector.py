from typing import Any, Dict, Set, Tuple, Callable, List

import torch
import torch.nn as nn
import torch.nn.qat as nnqat
from abc import ABC, abstractmethod
from torch.ao.quantization.fake_quantize import FakeQuantize
from torch.ao.quantization.fx.graph_module import GraphModule
from torch.ao.quantization.observer import ObserverBase
from torch.ao.quantization.fx._model_report.model_report_observer import ModelReportObserver
from torch.ao.quantization.qconfig import QConfig
from torch.ao.quantization.quantize import is_activation_post_process

# Names for observer insert keys
DETECTOR_TARGET_NODE_KEY = "target_node"
DETECTOR_OBS_TO_INSERT_KEY = "observer_to_insert"
DETECTOR_IS_POST_OBS_KEY = "is_post_observer"
DETECTOR_OBS_ARGS_KEY = "observer_args"

# Adding base class for detectors
class DetectorBase(ABC):
    r""" Base Detector Module
    Any detector class should derive from this class.

    Concrete detectors should follow the same general API, which includes:
    - A method to calculate and return observer insertion points
        - Should return both the fqns and the Observer class to insert
    - A method to return a report based on the the detector
        - Should return a str-based report and dict info in Tuple[str,Dict] format
    """

    def __init__(self):
        super().__init__()

    @abstractmethod
    def determine_observer_insert_points(self, model) -> Dict:
        r"""
        Args
            model (nn.Module or subclass): model to find observer insertion points

        Returns a Dict mapping from unique observer fqns (where we want to insert them) to a Dict.
            This dict maps string keys to detector specific information
        """
        pass

    @abstractmethod
    def get_detector_name(self) -> str:
        r""" Returns the name of the current detector """
        pass

    def _get_targeting_node(self, prepared_fx_model: GraphModule, target_fqn: str) -> torch.fx.node.Node:
        r"""
        Takes in a GraphModule and the target_fqn and finds the node whose target is this fqn.

        If it's not found, it means it is most likely inside a fused layer
            We just go one layer up in terms of the fqn we are searching for until we find parent node
            If we get to empty string, then we know that it doesn't exist

        The reason for the recursion is that if the model that we are looking for got fused,
        we will have module fqn as e.g. x.linear.0 but the graph will only have a node for the fused module,
        which would have fqn as x.linear so they will not match.
        To handle this, if we don't match, we then take off the last bit of the fqn e.g. x.linear.0 -> x.linear,
        or more generally foo.bar.baz -> foo.bar and search again, this will allow us to locate the correct module
        even in cases with fusion

        Args:
            prepared_fx_model (GraphModule):  The prepared Fx GraphModule
            target_fqn (str): The fqn of the layer we are trying to target

        Returns the node object we are trying to add observers around
        """
        for node in prepared_fx_model.graph.nodes:
            # if the node's target is our target, return it
            if node.target == target_fqn:
                return node

        # getting here means node not found
        # if no "." we are already at base and failed
        parent_fqn_sep_index = target_fqn.rfind(".")
        if parent_fqn_sep_index == -1:
            raise ValueError("passed in target_fqn not found in graph's targets.")
        else:
            # recursively call it with parent fqn
            return self._get_targeting_node(prepared_fx_model, target_fqn[:parent_fqn_sep_index])

    @abstractmethod
    def generate_detector_report(self, model) -> Tuple[str, Dict[str, Any]]:
        r"""
        Args
            model (nn.Module or subclass): model to find observer insertion points

        Returns a Tuple of two elements:
            Str: string report of the suggested improvements
            Dict: contains useful data collected by the observer pertinent to this report
        """
        pass

class PerChannelDetector(DetectorBase):
    r""" This class is used to detect if any Linear or Conv layers in a model utilize per_channel quantization.
        Only Linear and Conv layers can use per_channel as of now so only these two are currently checked.

        per_channel quantization can lead to major benefits in the form of accuracy.
        Therefore, if the backend used by the user supports it, it is recommended to use

        Args:
            backend (str, optional): the backend the user wishes to use in production
                Default value is current torch.backends.quantized.engine
    """

    # Default map for representing supported per channel quantization modules for different backends
    DEFAULT_BACKEND_PER_CHANNEL_SUPPORTED_MODULES: Dict[str, Set[Any]] = {
        "fbgemm": set([nn.Linear, nn.Conv1d, nn.Conv2d, nn.Conv3d, nnqat.Linear, nnqat.Conv1d, nnqat.Conv2d, nnqat.Conv3d]),
        "qnnpack": set([nn.Linear, nn.Conv1d, nn.Conv2d, nn.Conv3d, nnqat.Linear, nnqat.Conv1d, nnqat.Conv2d, nnqat.Conv3d]),
        "onednn": set([nn.Linear, nn.Conv1d, nn.Conv2d, nn.Conv3d, nnqat.Linear, nnqat.Conv1d, nnqat.Conv2d, nnqat.Conv3d]),
    }

    def __init__(self, backend: str = torch.backends.quantized.engine):
        super().__init__()

        # store the backend information
        self.backend_chosen = backend
        self.supported_modules = set([])
        if self.backend_chosen in self.DEFAULT_BACKEND_PER_CHANNEL_SUPPORTED_MODULES:
            self.supported_modules = self.DEFAULT_BACKEND_PER_CHANNEL_SUPPORTED_MODULES[self.backend_chosen]
        else:
            raise ValueError("Not configured to work with {}. Try a different default backend".format(self.backend_chosen))

    def get_detector_name(self) -> str:
        r""" returns the string name of this detector"""
        return "per_channel_detector"

    def determine_observer_insert_points(self, model: nn.Module) -> Dict:
        r"""
        There is no observers inserted for the PerChannelDetector.

        Returns an empty dictionary since no observers are added or needed
        """
        return {}


    def _detect_per_channel_helper(self, model: nn.Module, per_channel_info: Dict):
        r"""
        determines if per_channel quantization is supported in modules and submodules.

        Returns a dictionary in the higher level _detect_per_channel function.
        Each entry maps the fully-qualified-name to information on whether per_channel quantization.

        Args:
            module: The current module that is being checked to see if it is per_channel qunatizable

        Returns dictionary mapping fqns to if per_channel quantization is possible
        """
        for named_mod in model.named_modules():

            # get the fully qualified name and check if in list of modules to include and list of modules to ignore
            fqn, module = named_mod

            # asserts for MyPy
            assert isinstance(fqn, str) and isinstance(per_channel_info["per_channel_status"], dict)

            is_in_include_list = sum(list(map(lambda x: isinstance(module, x), self.supported_modules))) > 0

            # check if the module per_channel is supported
            # based on backend
            per_channel_supported = False

            if is_in_include_list:
                per_channel_supported = True

                # assert statement for MyPy
                q_config_file = module.qconfig
                assert isinstance(q_config_file, QConfig)

                # this object should either be fake quant or observer
                q_or_s_obj = module.qconfig.weight.p.func()
                assert isinstance(q_or_s_obj, FakeQuantize) or isinstance(q_or_s_obj, ObserverBase)

                per_channel_used = False  # will be true if found in qconfig

                if hasattr(q_or_s_obj, "ch_axis"):  # then we know that per_channel quantization used

                    # all fake quants have channel axis so need to check is_per_channel
                    if isinstance(q_or_s_obj, FakeQuantize):
                        if hasattr(q_or_s_obj, "is_per_channel") and q_or_s_obj.is_per_channel:
                            per_channel_used = True
                    elif isinstance(q_or_s_obj, ObserverBase):
                        # should be an observer otherwise
                        per_channel_used = True
                    else:
                        raise ValueError("Should be either observer or fake quant")

                per_channel_info["per_channel_status"][fqn] = {
                    "per_channel_supported": per_channel_supported,
                    "per_channel_used": per_channel_used,
                }

        return per_channel_info

    def generate_detector_report(self, model: nn.Module) -> Tuple[str, Dict[str, Any]]:
        r"""Checks if any Linear or Conv layers in the model utilize per_channel quantization.
        Only Linear and Conv layers can use per_channel as of now so only these two are currently checked.

        Looks at q_config format and backend to determine if per_channel can be utilized.
        Uses the DEFAULT_BACKEND_PER_CHANNEL_SUPPORTED_MODULES structure to determine support

        Args:
            model: The prepared and calibrated model we want to check if using per_channel

        Returns a tuple with two elements:
            String report of potential actions to improve model (if per_channel quantization is available in backend)
            Dictionary mapping per_channel quantizable elements to:
                whether per_channel quantization is supported by the backend
                if it is being utilized in the current model
        """

        # store information on submodules and if per_channel quantization is supported and used as well as qconfig information
        per_channel_info = {"backend": self.backend_chosen, "per_channel_status": {}}

        # run the helper function to populate the dictionary
        per_channel_info = self._detect_per_channel_helper(model, per_channel_info)

        # String to let the user know of further optimizations
        further_optims_str = "Further Optimizations for backend {}: \n".format(self.backend_chosen)

        # assert for MyPy check
        assert isinstance(per_channel_info["per_channel_status"], dict)

        optimizations_possible = False
        for fqn in per_channel_info["per_channel_status"]:
            fqn_dict = per_channel_info["per_channel_status"][fqn]
            if fqn_dict["per_channel_supported"] and not fqn_dict["per_channel_used"]:
                optimizations_possible = True
                further_optims_str += "Module {module_fqn} can be configured to use per_channel quantization.\n".format(
                    module_fqn=fqn
                )

        if optimizations_possible:
            further_optims_str += (
                "To use per_channel quantization, make sure the qconfig has a per_channel weight observer."
            )
        else:
            further_optims_str += "No further per_channel optimizations possible."

        # return the string and the dictionary form of same information
        return (further_optims_str, per_channel_info)


class DynamicStaticDetector(DetectorBase):
    r"""
    Determines whether dynamic or static quantization is more appropriate for a given module.

    Takes advantage of the ModelReportObserver that records range information.
    Stationary distribution of data are strictly above tolerance level for the comparison statistic:

        S = average_batch_activation_range/epoch_activation_range

    Nonstationary distributions are below or at the tolerance level for this metric.

    If the distribution of data right after the module is non-stationary, recommend dynamic quantization
        Otherwise recommend static quantization

    Args:
        tolerance (float, optional): The threshold where S metric is stationary above and non-stationary otherwise. Default: 0.5
    """
    # names for the pre and post observers that are inserted
    DEFAULT_PRE_OBSERVER_NAME = "model_report_pre_observer"
    DEFAULT_POST_OBSERVER_NAME = "model_report_post_observer"

    # naming conventions for stationary vs non-stationary data
    DEFAULT_STATIONARY = "stationary"
    DEFAULT_NON_STATIONARY = "non-stationary"

    # naming conventions for the keys of the return module info
    DEFAULT_TOLERANCE_KEY = "tolerance"
    DEFAULT_DYNAMIC_REC_KEY = "dynamic_recommended"
    DEFAULT_PRE_OBS_COMP_STAT_KEY = "pre_observer_comp_stat"
    DEFAULT_POST_OBS_COMP_STAT_KEY = "post_observer_comp_stat"
    DEFAULT_PRE_OBS_DATA_DIST_KEY = "pre_observer_data_dist"
    DEFAULT_POST_OBS_DATA_DIST_KEY = "post_observer_data_dist"

    # modules that are supported both dynamic and static for this report function
    DEFAULT_DYNAMIC_STATIC_CHECK_SUPPORTED = set([nn.Linear])

    def __init__(self, tolerance=0.5):
        super().__init__()

        # set tolerance level and initialize a set to keep track of useful fqn locations
        self.tolerance = tolerance
        self.useful_observer_fqns: Set[str] = set([])

    def determine_observer_insert_points(self, prepared_fx_model: GraphModule) -> Dict[str, Dict[str, Any]]:
        r"""
        Determines where observers need to be inserted for the Dynamic vs Static detector.
        For this detector, we want to place observers on either side of linear layers in the model.

        Currently inserts observers for:
            linear layers

        Args:
            prepared_fx_model (GraphModule):  The prepared Fx GraphModule

        Returns a Dict mapping from unique observer fqns (where we want to insert them) to a Dict with:
            key "target_node" -> the node we are trying to observe with this observer (torch.fx.node.Node)
            key "observer_to_insert" -> the observer we wish to insert (ObserverBase)
            key "is_post_observer" -> True if this is meant to be a post-observer for target_node, False if pre-observer
            key "observer_args" -> The arguments that are meant to be passed into the observer
        """

        # observer for this detector is ModelReportObserver
        obs_ctr = ModelReportObserver

        # return dict
        obs_fqn_to_info: Dict[str, Dict[str, Any]] = {}

        for fqn, module in prepared_fx_model.named_modules():
            # make sure module is supported
            if self._is_supported(module, insert=True):
                # if it's a supported type, we want to get node and add observer insert locations
                targeted_node = self._get_targeting_node(prepared_fx_model, fqn)

                # add entry for pre-observer
                pre_obs_fqn = fqn + "." + self.DEFAULT_PRE_OBSERVER_NAME

                obs_fqn_to_info[pre_obs_fqn] = {
                    DETECTOR_TARGET_NODE_KEY: targeted_node,
                    DETECTOR_OBS_TO_INSERT_KEY: obs_ctr(),
                    DETECTOR_IS_POST_OBS_KEY: False,
                    DETECTOR_OBS_ARGS_KEY: targeted_node.args
                }

                # add entry for post-observer
                post_obs_fqn = fqn + "." + self.DEFAULT_POST_OBSERVER_NAME

                obs_fqn_to_info[post_obs_fqn] = {
                    DETECTOR_TARGET_NODE_KEY: targeted_node,
                    DETECTOR_OBS_TO_INSERT_KEY: obs_ctr(),
                    DETECTOR_IS_POST_OBS_KEY: True,
                    DETECTOR_OBS_ARGS_KEY: (targeted_node,)
                }

        return obs_fqn_to_info

    def get_detector_name(self) -> str:
        r""" returns the string name of this detector"""
        return "dynamic_vs_static_detector"

    def _is_supported(self, module: nn.Module, insert: bool = False) -> bool:
        r"""Returns whether the given module is supported for observers

        Args
            module: The module to check and ensure is supported
            insert: True if this is check for observer insertion, false if for report gen

        Returns True if the module is supported by observer, False otherwise
        """
        # check to see if module is of a supported type
        is_supported_type = sum(list(map(lambda x: isinstance(module, x), self.DEFAULT_DYNAMIC_STATIC_CHECK_SUPPORTED))) > 0

        # this is check for observer insertion
        if insert:
            return is_supported_type
        else:
            # this is for report gen and we also need to check if it contains observers
            has_obs = hasattr(module, self.DEFAULT_PRE_OBSERVER_NAME) and hasattr(module, self.DEFAULT_POST_OBSERVER_NAME)
            return is_supported_type and has_obs

    def _generate_dict_info(self, model: GraphModule) -> Dict[str, Any]:
        r"""
        Helper function for generate_detector_report that does the generation of the dictionary.
        This process is done as specified in generate_detector_report documentation

        Args:
            model (GraphModule): The prepared and calibrated GraphModule with inserted ModelReportObservers

        Returns a Dictionary mapping modules with ModelReportObservers around them to:
                whether dynamic quantization is recommended
                their S metric of input to module
                whether input to module is stationary or non-stationary
                their S metric of output of module
                whether output of module is stationary or non-stationary
                the tolerance level to decided whether input/output is stationary or non-stationary
        """
        # store modules dynamic vs static information
        module_dynamic_static_info = {}

        # This for loop goes through the modules, and extracts all relavent information into module_dynamic_static_info
        #   This information primary includes whether the data distributions around a supported module is stationary or not
        #   Based on this, it is recorded whether dynamic or static quantization is recommended

        # loop through all submodules included nested ones
        for fqn, module in model.named_modules():
            # if module is Linear has the ModelReportObserver attached to it
            if self._is_supported(module):
                # get pre and post observers for the module
                pre_obs = getattr(module, self.DEFAULT_PRE_OBSERVER_NAME)
                post_obs = getattr(module, self.DEFAULT_POST_OBSERVER_NAME)

                # get the statistics for each module
                pre_stat = pre_obs.get_batch_to_epoch_ratio()
                post_stat = post_obs.get_batch_to_epoch_ratio()

                # record module, pre and post stat, and whether to do dynamic or static based off it
                # true if post observer data distribution is non-stationary, false if it's stationary
                dynamic_recommended = post_stat <= self.tolerance

                # specify the classifications for whether data distributions considered stationary or non-stationary
                pre_obs_dist_classif = self.DEFAULT_STATIONARY if pre_stat > self.tolerance else self.DEFAULT_NON_STATIONARY
                post_obs_dist_classif = self.DEFAULT_STATIONARY if post_stat > self.tolerance else self.DEFAULT_NON_STATIONARY

                # store the set of important information for this module
                module_info = {
                    self.DEFAULT_TOLERANCE_KEY: self.tolerance,
                    self.DEFAULT_DYNAMIC_REC_KEY: dynamic_recommended,
                    self.DEFAULT_PRE_OBS_COMP_STAT_KEY: pre_stat,
                    self.DEFAULT_PRE_OBS_DATA_DIST_KEY: pre_obs_dist_classif,
                    self.DEFAULT_POST_OBS_COMP_STAT_KEY: post_stat,
                    self.DEFAULT_POST_OBS_DATA_DIST_KEY: post_obs_dist_classif,
                }

                module_dynamic_static_info[fqn] = module_info

        return module_dynamic_static_info

    def generate_detector_report(self, model: GraphModule) -> Tuple[str, Dict[str, Any]]:
        r"""
        Determines whether dynamic or static quantization is more appropriate for a given module.

        Takes advantage of the ModelReportObserver that records range information.
        Stationary distribution of data are strictly above tolerance level for the comparison statistic:

            S = average_batch_activation_range/epoch_activation_range

        Nonstationary distributions are below or at the tolerance level for this metric.

        If the distribution of data right after the module is non-stationary, recommend dynamic quantization
            Otherwise recommend static quantization

        This will then generate suggestions for dynamic vs static quantization focused around Linear.

        Args:
            model (GraphModule): The prepared and calibrated GraphModule with inserted ModelReportObservers

        Returns a tuple with two elements:
            String report of of whether dynamic or static quantization is recommended for certain modules
            Dictionary mapping modules with ModelReportObservers around them to:
                whether dynamic quantization is recommended
                their S metric of input to module
                whether input to module is stationary or non-stationary
                their S metric of output of module
                whether output of module is stationary or non-stationary
                the tolerance level to decided whether input/output is stationary or non-stationary
        """

        # get the dictionary of the information to format the string report
        module_dynamic_static_info = self._generate_dict_info(model)

        dynamic_vs_static_string = "Dynamic vs. Static Quantization suggestions: \n"

        modules_added: bool = False  # check to make sure at least 1 module added.

        # This for loop goes through the information collected in module_dynamic_static_info and:
        #   Populates the string based report with the information from module_dynamic_static_info
        #   Compiles the complete report by appending relavent formatted strings

        for module_fqn in module_dynamic_static_info.keys():

            # there is at least 1 module for suggestion
            modules_added = True
            module_info = module_dynamic_static_info[module_fqn]
            suggestion_string_template = "For module {} it is suggested to use {} quantization because {}.\n"

            # decide what string formatting values will be
            quantization_type = ""

            quantization_reasoning = "the distribution of data before {} is {} and the distribution after is {}."
            dynamic_benefit = " You will get more accurate results if you use dynamic quantization"
            static_benefit = " You can increase model efficiency if you use static quantization"
            benefit_str = ""

            # strings for if dynamic quantized per tensor is needed
            recommend_per_tensor = " We recommend to add a {} before this module if it is static."
            rec_lay_to_add = "dynamic quantize per tensor layer"
            dynamic_per_tensor_string = recommend_per_tensor.format(rec_lay_to_add)
            dynamic_per_tensor_reasoning_string = (
                " This is because the input to this module has a non-stationary distribution"
            )

            # start composing explanation
            if module_info[self.DEFAULT_DYNAMIC_REC_KEY]:
                quantization_type = "dynamic"
                benefit_str = dynamic_benefit
            else:
                quantization_type = "static"
                benefit_str = static_benefit

            # now set the quantization explanation string
            quantization_reasoning = (
                quantization_reasoning.format(
                    module_fqn, module_info[self.DEFAULT_PRE_OBS_DATA_DIST_KEY], module_info[self.DEFAULT_POST_OBS_DATA_DIST_KEY]
                )
                + benefit_str
            )

            # if we have a non-stationary input -> linear -> stationary we suggested static
            # however, we want to also recommend they add a dynamic quantize per tensor right if this change is made
            if (
                module_info[self.DEFAULT_PRE_OBS_DATA_DIST_KEY] == self.DEFAULT_NON_STATIONARY
                and module_info[self.DEFAULT_POST_OBS_DATA_DIST_KEY] == self.DEFAULT_STATIONARY
            ):
                quantization_reasoning = (
                    quantization_reasoning + dynamic_per_tensor_string + dynamic_per_tensor_reasoning_string
                )

            # format the overall suggestion string with the specific inputs
            module_suggestion_string = suggestion_string_template.format(
                module_fqn, quantization_type, quantization_reasoning
            )

            # append to overall suggestion
            dynamic_vs_static_string += module_suggestion_string

        if not modules_added:
            dynamic_vs_static_string += "No applicable layers for suggestions. Only linear valid.\n"

        # return the string as well as the dictionary of information
        return (dynamic_vs_static_string, module_dynamic_static_info)


class InputWeightEqualizationDetector(DetectorBase):
    r"""
    Determines whether input-weight equalization can help improve quantization for certain modules.

    Specifically, this list of modules includes:
        linear
        conv

    Determines whether input-weight equalization is recommended based on the comp stat:
        s_c = sqrt(w_c/W)/sqrt(i_c/I)
        where:
            w_c is range of weight for channel c, W is range of weight over all channels
            i_c is range of input for channel c, I is range of input over all channels

        if s_c >= threshold or <= 1 / threshold, recommends input-weight equalization

    Args:
        ratio_threshold (float): The threshold for s_c to determine if input-weight equalization is sugggested
            Should be between 0 and 1 (both non-inclusive)
        ch_axis (int, optional): The channel axis being observed to determine input weight equalization
            Default: 1

    * :attr:`ratio_threshold`: The threshold for s_c to determine if input-weight equalization is sugggested
        Should be between 0 and 1

    * :attr:`ch_axis`: The channel axis being observed to determine input weight equalization

    * :attr:`SUPPORTED_MODULES`: This specifies the modules that are supported for input-weight equalization

    * :attr:`DEFAULT_PRE_OBSERVER_NAME`: The name of the pre-observer to be inserted for this detector
    """

    SUPPORTED_MODULES: Set[Callable] = set(
        [nn.Linear, nn.Conv1d, nn.Conv2d, nn.Conv3d, nnqat.Linear, nnqat.Conv1d, nnqat.Conv2d, nnqat.Conv3d]
    )

    # names for the pre and post observers that are inserted
    DEFAULT_PRE_OBSERVER_NAME: str = "model_report_pre_observer"
    DEFAULT_POST_OBSERVER_NAME: str = "model_report_post_observer"

    # string names for keys of info dictionaries
    PER_CHANNEL_MAX_KEY = "per_channel_max"
    PER_CHANNEL_MIN_KEY = "per_channel_min"
    GLOBAL_MAX_KEY = "global_max"
    GLOBAL_MIN_KEY = "global_min"

    # keys for return dict of recommendations
    RECOMMENDED_KEY = "input_weight_equalization_recommended"
    COMP_METRIC_KEY = "channel_comparison_metrics"
    THRESHOLD_KEY = "threshold"
    CHANNEL_KEY = "channel_axis_selected"
    INPUT_INFO_KEY = "input_range_info"
    WEIGHT_INFO_KEY = "weight_range_info"

    # default weight and info strings
    WEIGHT_STR = "weight"
    INPUT_STR = "input"

    def __init__(self, ratio_threshold: float, ch_axis: int = 1):
        # ensure passed in inputs are valid
        if ratio_threshold <= 0 or ratio_threshold >= 1:
            raise ValueError("Make sure threshold is > 0 and < 1")

        # intialize attributes based on args
        self.ratio_threshold: float = ratio_threshold
        self.ch_axis: int = ch_axis

    def _is_supported(self, module: nn.Module, insert: bool = False) -> bool:
        r"""Returns whether the given module is supported for observers

        Args
            module: The module to check and ensure is supported
            insert: True if this is check for observer insertion, false if for report gen

        Returns True if the module is supported by observer, False otherwise
        """
        # check to see if module is of a supported type
        is_supported_type = sum(list(map(lambda x: type(module) is x, self.SUPPORTED_MODULES))) > 0

        # this is check for observer insertion
        if insert:
            return is_supported_type
        else:
            # this is for report gen and we also need to check if it contains observers
            has_obs = hasattr(module, self.DEFAULT_PRE_OBSERVER_NAME)
            return is_supported_type and has_obs

    def determine_observer_insert_points(self, prepared_fx_model: GraphModule) -> Dict[str, Dict[str, Any]]:
        r"""Determines where observers need to be inserted for the Input Weight Equalization Detector.
        For this detector, we want to place observers in front of supported layers.

        Currently inserts observers for:
            linear layers
            conv layers

        Args:
            prepared_fx_model (GraphModule):  The prepared Fx GraphModule

        Returns a Dict mapping from unique observer fqns (where we want to insert them) to a Dict with:
            key "target_node" -> the node we are trying to observe with this observer (torch.fx.node.Node)
            key "observer_to_insert" -> the observer we wish to insert (ObserverBase)
            key "is_post_observer" -> True if this is meant to be a post-observer for target_node, False if pre-observer
            key "observer_args" -> The arguments that are meant to be passed into the observer
        """

        # observer for this detector is ModelReportObserver
        obs_ctr = ModelReportObserver

        # return dict
        obs_fqn_to_info: Dict[str, Dict[str, Any]] = {}

        for fqn, module in prepared_fx_model.named_modules():
            # check to see if module is of a supported type
            if self._is_supported(module, insert=True):
                # if it's a supported type, we want to get node and add observer insert locations
                targeted_node = self._get_targeting_node(prepared_fx_model, fqn)

                # add entry for pre-observer
                pre_obs_fqn = fqn + "." + self.DEFAULT_PRE_OBSERVER_NAME

                obs_fqn_to_info[pre_obs_fqn] = {
                    DETECTOR_TARGET_NODE_KEY: targeted_node,
                    DETECTOR_OBS_TO_INSERT_KEY: obs_ctr(ch_axis=self.ch_axis),
                    DETECTOR_IS_POST_OBS_KEY: False,
                    DETECTOR_OBS_ARGS_KEY: targeted_node.args,
                }

        return obs_fqn_to_info

    def get_detector_name(self) -> str:
        r"""Returns the name of this detector"""
        return "input_weight_equalization_detector"

    def _extract_input_info(self, model: GraphModule) -> Dict[str, Dict]:
        r"""
        Takes in a callibrated GraphModule and then finds the relevant observers.
        It then extracts the input information for each observer returns it

        Args
            model (GraphModule): The prepared and calibrated GraphModule with inserted ModelReportObservers

        Returns a dict mapping relavent module fqns (str) to a dict with keys:
            "per_channel_max" : maps to the per_channel max values
            "per_channel_min" : maps to the per_channel min values
            "global_max" : maps to the global max recorded
            "global_min" : maps to the global min recorded
        """

        # return dictionary mapping observer fqns to desired info
        input_info: Dict[str, Dict] = {}

        for fqn, module in model.named_modules():
            # if module is supported and it has a pre-observer
            if self._is_supported(module):
                # get pre observer for the module
                pre_obs = getattr(module, self.DEFAULT_PRE_OBSERVER_NAME)

                input_info[fqn] = {
                    self.PER_CHANNEL_MAX_KEY: pre_obs.max_val,
                    self.PER_CHANNEL_MIN_KEY: pre_obs.min_val,
                    self.GLOBAL_MAX_KEY: max(pre_obs.max_val),
                    self.GLOBAL_MIN_KEY: min(pre_obs.min_val),
                }

        return input_info

    def _extract_weight_info(self, model: GraphModule) -> Dict[str, Dict]:
        r"""
        Takes in a callibrated GraphModule and then finds the relavent observers.
        It then extracts the weight information for each layer an observer is attached to.

        Args
            model (GraphModule): The prepared and calibrated GraphModule with inserted ModelReportObservers

        Returns a dict mapping module fqns (str) to a dict with keys:
            "per_channel_max" : maps to the per_channel max values
            "per_channel_min" : maps to the per_channel min values
            "global_max" : maps to the global max recorded
            "global_min" : maps to the global min recorded
        """
        # return dictionary mapping observer fqns to desired info
        weight_info: Dict[str, Dict] = {}

        for fqn, module in model.named_modules():
            # if module is supported and it has a pre-observer
            if self._is_supported(module):
                # we don't need actual observer, just the module weights
                # calculate min and max vals
                min_val, max_val = torch.aminmax(module.weight, dim=self.ch_axis)

                # flatten entries since conv can have multiple dimensions
                min_val = torch.flatten(min_val)
                max_val = torch.flatten(max_val)

                weight_info[fqn] = {
                    self.PER_CHANNEL_MAX_KEY: max_val,
                    self.PER_CHANNEL_MIN_KEY: min_val,
                    self.GLOBAL_MAX_KEY: max(max_val),
                    self.GLOBAL_MIN_KEY: min(min_val),
                }

        return weight_info

    def _calculate_range_ratio(self, info_dict: Dict, info_str: str, module_fqn: str) -> torch.Tensor:
        r"""
        Takes in an info dict and calculates the s_c matrix.

        Args:
            info_dict (dict): A dictionary of either input or weight range info
            info_str (str): A str describing whether currently looking at weight or input info
                Either "weight" or "input"
            module_fqn (str): The fqn of the module we are looking at

        Returns a tensor of values, where each value is the s_c stat for a different channel
        """
        # calculate the ratios of the info
        per_channel_range = info_dict[self.PER_CHANNEL_MAX_KEY] - info_dict[self.PER_CHANNEL_MIN_KEY]
        global_range = info_dict[self.GLOBAL_MAX_KEY] - info_dict[self.GLOBAL_MIN_KEY]

        if global_range == 0:
            range_zero_explanation = "We recommend removing this channel as it doesn't provide any useful information."
            raise ValueError(
                "The range of the {} data for module {} is 0, which means you have a constant value channel. {}".format(
                    info_str, module_fqn, range_zero_explanation
                )
            )

        ratio = per_channel_range / global_range

        return ratio

    def _generate_comparision_values(self, input_info: Dict, weight_info: Dict) -> Dict[str, torch.Tensor]:
        r"""
        Takes in the information on the min and max values of the inputs and weights and:
            Calculates the comp stat for each channel: s_c = sqrt(w_c/W)/sqrt(i_c/I)

        Args:
            input_info (dict): A dict mapping each observer to input range information
            weight_info (dict): A dict mapping each observer to weight range information

        Returns a dict mapping relavent observer fqns (str) to a 1-D tensor.
            Each value is a different s_c value for a different channel
        """
        # create return dictionary for each observer
        module_fqn_to_channel: Dict[str, torch.Tensor] = {}

        # for each module (both passed in dicts should have same keys)
        for module_fqn in input_info:

            # raise error if not in weight info
            if module_fqn not in weight_info:
                raise KeyError("Unable to find weight range stats for module {}".format(module_fqn))

            # calculate the ratios of the weight info and input info
            weight_ratio = self._calculate_range_ratio(weight_info[module_fqn], self.WEIGHT_STR, module_fqn)
            input_ratio = self._calculate_range_ratio(input_info[module_fqn], self.INPUT_STR, module_fqn)

            # calculate the s metric per channel
            s = torch.sqrt(weight_ratio) / torch.sqrt(input_ratio)

            # add to dictionary
            module_fqn_to_channel[module_fqn] = s

        # return compiled observer ratios
        return module_fqn_to_channel

    def _generate_dict_info(self, input_info: Dict, weight_info: Dict, comp_stats: Dict) -> Dict[str, Dict]:
        r"""
        Helper function for generate_detector_report that does the generation of the dictionary.
        This process is done as specified in generate_detector_report documentation

        Args:
            input_info (dict): A dict mapping each module to input range information
            weight_info (dict): A dict mapping each module to weight range information
            comp_stats (dict): A dict mapping each module to its corresponding comp stat

        Returns a dictionary mapping each module with relavent ModelReportObservers around them to:
            whether input weight equalization is recommended
            their s_c metric compared to the threshold
            the threshold used to make the recommendation
            the channel used for recording data
            the input channel range info
            the weight channel range info
        """
        # store modules input weight equalization info
        input_weight_equalization_info: Dict[str, Dict] = {}

        # for each module we add seperate set of suggestions
        for module_fqn in input_info:

            # get relavent info for this module
            mod_input_info: Dict = input_info[module_fqn]
            mod_weight_info: Dict = weight_info[module_fqn]
            mod_comp_stat: Dict = comp_stats[module_fqn]

            # decide if each channel should have input weight equalization or not
            channel_rec_vals: list = []

            for val in mod_comp_stat:
                float_rep: float = val.item()

                # decide if recommending input weight equalization
                recommended: bool = float_rep >= self.ratio_threshold and float_rep <= 1 / self.ratio_threshold
                channel_rec_vals.append(recommended)

            # build the return dict input
            input_weight_equalization_info[module_fqn] = {
                self.RECOMMENDED_KEY: channel_rec_vals,
                self.COMP_METRIC_KEY: mod_comp_stat,
                self.THRESHOLD_KEY: self.ratio_threshold,
                self.CHANNEL_KEY: self.ch_axis,
                self.INPUT_INFO_KEY: mod_input_info,
                self.WEIGHT_INFO_KEY: mod_weight_info,
            }

        # return our compiled info for each module
        return input_weight_equalization_info

    def generate_detector_report(self, model: GraphModule) -> Tuple[str, Dict[str, Any]]:
        r"""
        Determines whether input weight equalization is appropriate for a given module.

        Takes advantage of the ModelReport Observer which records per channel information of input range
        It then uses the passed in weight info inconjunction to compute the desired ratio
        Finally, it gives suggestions based on this information for each module of interest

        Args:
            model (GraphModule): The prepared and calibrated GraphModule with inserted ModelReportObservers
            weight_info (Dict): Maps modules of interest to information on their weights to be analyzed

        Returns a tuple with two elements:
            String report of of whether input weight equalization is recommended for certain modules
            Dictionary mapping modules of interest to:
                whether input weight equalization is recommended
                their s_c metric compared to the threshold
                the threshold used to make the recommendation
                the channel used for recording data
                the input channel range info
                the weight channel range info
        """

        # find the range of inputs
        input_values: Dict[str, Dict] = self._extract_input_info(model)

        # find the range of weights
        weight_values: Dict[str, Dict] = self._extract_weight_info(model)

        # calculate per_channel comparision statistic s_c
        comp_stats: Dict[str, torch.Tensor] = self._generate_comparision_values(input_values, weight_values)

        # generate the return dictionary
        input_weight_equalization_info: Dict[str, Dict] = self._generate_dict_info(input_values, weight_values, comp_stats)

        # now we can generate report based on this information
        input_weight_string = "Input-Weight Equalization suggestions: \n"

        # some strings to be formatted depending on module we are adding
        module_suggestion_str = "For Module {} looked at with axis {} we suggest: \n"
        channel_suggestion_str = "\tFor channel {}, we suggest {} input weight equalization because {}\n"
        use_str = "to use"
        no_use_str = "to not use"
        input_weight_benefit_str = "we expect significant reduction in quantization error."
        input_weight_non_benefit_reasoning = "the scales of the input vs. weight with regards to their ranges."
        input_weight_non_benefit_str = "we don't expect much improvement from input-weight equalization based on {}"

        # added module check
        added_module: bool = False

        # compile the suggestion string
        for module_fqn in input_weight_equalization_info:
            # we added at least 1 module
            added_module = True
            # add the module level description
            input_weight_string += module_suggestion_str.format(module_fqn, self.ch_axis)

            mod_info: Dict[str, Any] = input_weight_equalization_info[module_fqn]

            # look at each individual channel and add a suggestion
            for index, channel_suggested in enumerate(mod_info[self.RECOMMENDED_KEY]):
                if channel_suggested:
                    channel_str = channel_suggestion_str.format(index, use_str, input_weight_benefit_str)
                    input_weight_string += channel_str
                else:
                    non_benefit_str = input_weight_non_benefit_str.format(input_weight_non_benefit_reasoning)
                    channel_str = channel_suggestion_str.format(index, no_use_str, non_benefit_str)
                    input_weight_string += channel_str

        # if no modules looked at, amend return string
        if not added_module:
            input_weight_string += "No applicable layers for suggestions. Only linear and conv valid.\n"

        # return a tuple with the string explanation and the compiled dict info
        return (input_weight_string, input_weight_equalization_info)


class OutlierDetector(DetectorBase):
    r"""
    Determines whether there are significant outliers in activation data around a certain layer.

    This is ideally used in conjunction with information on stationary vs. non-stationary distribution:
        If the data is stationary, and there are significant outliers, then we want to flag them
        We want to do this on a per channel basis for detecting outliers

    Determines whether activation data is flagged as outlier based on if data is stationary and:
        p_r = avg(100th percentile / "reference_percentile"th percentile)
        where:
            p_r is average percentile ratio across all batches in the epoch
            reference_percentile is a percentile values between 0 and 100 exclusive

        if p_r is above some threshold, then we consider the activations to have significant outliers

    Args:
        ratio_threshold (float, optional): The threshold for p_r to determine if there are outliers in activations
            Should be >= 1
            Default: 3.5
        reference_percentile (float, optional): The denominator to find the relative scale of the 100th percentile
            Should be between 0 and 1
            Default: 0.975
        fraction_batches_used_threshold (float, optional): Threshold of fraction of batches per channel to determine outlier
            If fraction is below this, we deem number of samples used to calculate outliers as insignificant and alert user
            regardless of whether we detected outliers or not in channel to take a closer look at channel results
            Should be between 0 and 1
            Default: 0.95
        ch_axis (int, optional): The channel axis being observed to determine input weight equalization
            Default: 1

    * :attr:`ratio_threshold`: The threshold for p_r to determine if there are outliers in activations
        The p_r value (average ratio of 100th percentile/reference_percentile) is compared to ratio_threshold
        If it is significantly greater, then we consider it an outlier
        This threshold was calculated based on the ratio of the percentiles in a normal distribution
        The calculations behind value choice: https://drive.google.com/file/d/1N2wdtXWI-kOH8S7HH4-PYB_NmqzZil4p/view?usp=sharing

    * :attr:`reference_percentile`: The denominator of the top fraction to find the relative scale of the 100th percentile
        Should be between 0 and 1
        The calculations behind value choice: https://drive.google.com/file/d/1N2wdtXWI-kOH8S7HH4-PYB_NmqzZil4p/view?usp=sharing

    * :attr:`fraction_batches_used_threshold`: The fraction of batches to determine outliers for each channel should be above this
        Some batches may not be used because of 0-based errors, so this is to ensure a good amount of the total batches are used
        Should be between 0 and 1

    * :attr:`ch_axis`: The channel axis being observed to determine outliers

    * :attr:`DEFAULT_PRE_OBSERVER_NAME`: The name of the pre-observer to be inserted for this detector
    """

    # names for the pre observers that are inserted
    DEFAULT_PRE_OBSERVER_NAME: str = "model_report_pre_observer"

    # names for dict keys
    OUTLIER_KEY = "outliers_detected"
    NUM_BATCHES_KEY = "batches_used"
    IS_SUFFICIENT_BATCHES_KEY = "is_sufficient_batches"
    COMP_METRIC_KEY = "percentile_ratios"
    RATIO_THRES_KEY = "ratio_threshold"
    REF_PERCENTILE_KEY = "reference_percentile"
    CHANNEL_AXIS_KEY = "channel_axis"
    MAX_VALS_KEY = "per_channel_max"
    CONSTANT_COUNTS_KEY = "constant_batch_counts"

    def __init__(
        self,
        ratio_threshold: float = 3.5,
        reference_percentile: float = 0.975,
        fraction_batches_used_threshold: float = 0.95,
        ch_axis: int = 1,
    ):
        # initialize the variables of interest
        self.ratio_threshold = ratio_threshold

        # make sure passed in percentile is valid
        assert reference_percentile >= 0 and reference_percentile <= 1
        assert fraction_batches_used_threshold >= 0 and fraction_batches_used_threshold <= 1
        self.reference_percentile = reference_percentile
        self.fraction_batches_used_threshold = fraction_batches_used_threshold
        self.ch_axis = ch_axis

    def get_detector_name(self) -> str:
        r"""Returns the name of this detector"""
        return "outlier_detector"

    def _supports_insertion(self, module: nn.Module) -> bool:
        r"""Returns whether the given module is supported for observers insertion

        Any module that doesn't have children and isn't an observer itself is supported

        Args
            module: The module to check and ensure is supported

        Returns True if the module is supported by observer, False otherwise
        """
        # case for insertion of module
        # check if the module has any children and isn't observer
        num_children = len(list(module.children()))
        return num_children == 0 and not is_activation_post_process(module)

    def _supports_report_gen(self, module: nn.Module) -> bool:
        r"""Returns whether the given module is supported for report generation

        Any module that has a model report pre-observer is supported

        Args
            module: The module to check and ensure is supported

        Returns True if the module is supported by observer, False otherwise
        """
        return hasattr(module, self.DEFAULT_PRE_OBSERVER_NAME)

    def determine_observer_insert_points(self, prepared_fx_model: GraphModule) -> Dict[str, Dict[str, Any]]:
        r""" Determines where observers need to be inserted for the Outlier Detector.

        For this detector, we want to place observers in front of supported layers.

        Currently inserts observers for:
            all layers that do not have children (leaf level layers)

        Args:
            prepared_fx_model (GraphModule):  The prepared Fx GraphModule

        Returns a Dict mapping from unique observer fqns (where we want to insert them) to a Dict with:
            key "target_node" -> the node we are trying to observe with this observer (torch.fx.node.Node)
            key "observer_to_insert" -> the observer we wish to insert (ObserverBase)
            key "is_post_observer" -> True if this is meant to be a post-observer for target_node, False if pre-observer
            key "observer_args" -> The arguments that are meant to be passed into the observer
        """
        # observer for this detector is ModelReportObserver
        obs_ctr = ModelReportObserver

        # return dict
        obs_fqn_to_info: Dict[str, Dict[str, Any]] = {}

        for fqn, module in prepared_fx_model.named_modules():
            # check to see if module is of a supported type
            if self._supports_insertion(module):
                # if it's a supported type, we want to get node and add observer insert locations
                targeted_node = self._get_targeting_node(prepared_fx_model, fqn)

                # add entry for pre-observer
                pre_obs_fqn = fqn + "." + self.DEFAULT_PRE_OBSERVER_NAME

                obs_fqn_to_info[pre_obs_fqn] = {
                    DETECTOR_TARGET_NODE_KEY: targeted_node,
                    DETECTOR_OBS_TO_INSERT_KEY: obs_ctr(ch_axis=self.ch_axis, comp_percentile=self.reference_percentile),
                    DETECTOR_IS_POST_OBS_KEY: False,
                    DETECTOR_OBS_ARGS_KEY: targeted_node.args,
                }

        return obs_fqn_to_info

    def _calculate_outlier_info(
        self,
        percentile_ratios: torch.Tensor,
        counted_batches: torch.Tensor,
        total_batches: int,
    ) -> Dict[str, List[bool]]:
        r"""
        Gives info on whether the percentile ratios cacluated would be considered outliers
        Also gives information on whether the collected data is statistically significant to make this claim

        Args:
            percentile_ratios (torch.Tensor): The average percentile_ratios per channel calculated by the observer
            counted_batches (torch.Tensor): The number of batches used for average calculation per tensor
            total_batches (int): The total number of batches that passed through observer in this epoch

        Returns a dictionary mapping:
            "outliers_detected" : list of bools per channel that are true if it is considered an outlier
            "is_sufficient_batches": if o_r was >= fraction_batches_used_threshold:
                where o_r = counted_batches / total_batches
        """
        outlier_dict: Dict[str, List[bool]] = {self.OUTLIER_KEY: [], self.IS_SUFFICIENT_BATCHES_KEY: []}

        # get both as flattened lists for easy mapping
        ratios_list: List = percentile_ratios.tolist()
        num_batches_list: List = counted_batches.tolist()

        # calculate whether channels were statistically significant
        significant_size = [
            batch_size / total_batches >= self.fraction_batches_used_threshold for batch_size in num_batches_list
        ]
        outlier_dict[self.IS_SUFFICIENT_BATCHES_KEY] = significant_size

        # calculate for each channel whether it's an outlier or not based on ratio
        outlier_detected = [ratio > self.ratio_threshold for ratio in ratios_list]
        outlier_dict[self.OUTLIER_KEY] = outlier_detected

        # return the dictionary with the two lists
        return outlier_dict

    def _generate_info_dict(self, model: GraphModule) -> Dict[str, Dict]:
        r"""
        Helper function for generate_detector_report that does the generation of the dictionary.
        This process is done as specified in generate_detector_report documentation

        Args:
            model (GraphModule): The prepared and calibrated GraphModule with inserted ModelReportObservers

        Returns a dict mapping relavent module fqns to:
            whether there were outliers found in activation before
            the number of batches used for each channel
            whether fraction of applicable batches used is above fraction_batches_used_threshold
            their p_r metric compared to the threshold
            the threshold used to make the recommendation
            the reference_percentile used to make the recommendation
            the channel axis used to determine individual channels
            the constant batch counts per channel
            the per channel max values
        """
        # return dictionary mapping observer fqns to desired info
        info_dict: Dict[str, Dict] = {}

        for fqn, module in model.named_modules():
            # if module is supported and it has a pre-observer
            if self._supports_report_gen(module):
                # get pre observer for the module
                pre_obs: ModelReportObserver = getattr(module, self.DEFAULT_PRE_OBSERVER_NAME)

                # get the number of batches and calculated ratio thresholds
                num_batches: torch.Tensor = pre_obs.percentile_batches_tracked
                average_ratios: torch.Tensor = pre_obs.average_percentile_ratio
                channel_batch_cnts: torch.Tensor = pre_obs.constant_channels
                total_batches: int = pre_obs.num_batches_tracked

                # also get the max values
                max_vals: torch.Tensor = pre_obs.max_val

                # we have to specifically modify how we are recording negative ratio for pre-relu layers
                for index, ratio_val in enumerate(average_ratios):
                    # check if we have a negative ratio
                    # a ratio might be negative if we have a situation where the 100th percentile is
                    # > 0 while the nth percentile is < 0, in which case this would not be detected
                    # as an outlier. Since we care more about magnitude, we make it positive.
                    if ratio_val.item() < 0:
                        # first make it positive
                        average_ratios[index] = -ratio_val

                    if ratio_val.item() < 1:
                        # if it's less than 1 we have the flip it as well
                        average_ratios[index] = 1 / ratio_val

                outlier_calcs = self._calculate_outlier_info(average_ratios, num_batches, total_batches)

                # calculate whether ratios were outliers
                info_dict[fqn] = {
                    self.CHANNEL_AXIS_KEY: self.ch_axis,
                    self.REF_PERCENTILE_KEY: self.reference_percentile,
                    self.RATIO_THRES_KEY: self.ratio_threshold,
                    self.COMP_METRIC_KEY: average_ratios,
                    self.NUM_BATCHES_KEY: num_batches,
                    self.OUTLIER_KEY: outlier_calcs[self.OUTLIER_KEY],
                    self.IS_SUFFICIENT_BATCHES_KEY: outlier_calcs[self.IS_SUFFICIENT_BATCHES_KEY],
                    self.CONSTANT_COUNTS_KEY: channel_batch_cnts,
                    self.MAX_VALS_KEY: max_vals
                }

        return info_dict

    def generate_detector_report(self, model: GraphModule) -> Tuple[str, Dict[str, Any]]:
        r"""
        Determines whether input weight equalization is appropriate for a given module.

        Takes advantage of the ModelReport Observer which records the relavent percentile information

        Args:
            model (GraphModule): The prepared and calibrated GraphModule with inserted ModelReportObservers

        Returns a tuple with two elements:
            String report of of whether there are outliers in the activations around certain modules
            Dictionary mapping modules of interest to:
                whether there were outliers found in activation before
                the number of batches used for each channel
                whether fraction of applicable batches used is above fraction_batches_used_threshold
                their p_r metric compared to the threshold
                the threshold used to make the recommendation
                the reference_percentile used to make the recommendation
                the channel axis used to determine individual channels
                the constant batch counts per channel
                the per channel max values
        """
        # generate the information dictionary of outlier information
        info_dict = self._generate_info_dict(model)

        # now we can generate report based on this information
        outlier_string = "Outlier detection report: \n"

        # added module check
        added_module: bool = False

        # some strings to be formatted depending on module we are adding
        module_suggestion_str = "For Module {} looked at with axis {}: \n"
        channel_suggestion_str = "\tFor channel {}, we found outliers in the preceding activation data with {}.\n"
        channel_max_value_str = "a max value across all batches of {}"
        note_string = "Note: outlier detection is only reliable for {}. We recommend {} to ensure the most accurate results."
        note_distribution = "stationary distributions"
        note_rec = "running the static vs. dynamic detector to ensure activation data before modules above is stationary"

        # suggestion for constant batch check since that can make it no outliers
        constant_str = "\tFor channel {}, we found {} constant value batches. {}\n"
        constant_suggestion = "We recommend taking a look at the dict and data to see how frequent this occured and why."

        # compile the suggestion string
        for module_fqn in info_dict:
            # get module specific info
            mod_info: Dict[str, Any] = info_dict[module_fqn]
            # check to see if we already added high level model desc
            added_model_desc = False
            # look at each individual channel and add a suggestion
            for index, outlier_detected in enumerate(mod_info[self.OUTLIER_KEY]):
                if outlier_detected:
                    # we found at least 1 outlier
                    if not added_model_desc:
                        # add the module level description
                        outlier_string += module_suggestion_str.format(module_fqn, self.ch_axis)
                        added_model_desc = True

                    # we mark that we found at least one outlier
                    added_module = True
                    max_value_found_str = channel_max_value_str.format(mod_info[self.MAX_VALS_KEY][index])
                    channel_str = channel_suggestion_str.format(index, max_value_found_str)
                    outlier_string += channel_str

                # also check if we found constant batch
                if mod_info[self.CONSTANT_COUNTS_KEY][index] != 0:
                    # make sure we add a module level highlight.
                    if not added_model_desc:
                        # add the module level description
                        outlier_string += module_suggestion_str.format(module_fqn, self.ch_axis)
                        added_model_desc = True

                    constant_values_for_channel = mod_info[self.CONSTANT_COUNTS_KEY][index]
                    formatted_str = constant_str.format(index, constant_values_for_channel, constant_suggestion)
                    outlier_string += formatted_str
                    # we also added at least one thing to description
                    added_module = True


        # if found outlier, give suggestion, else give default response
        if added_module:
            # compose the note string
            note_composed = note_string.format(note_distribution, note_rec)
            outlier_string += note_composed
        else:
            outlier_string += "There were no outliers found in the activations.\n"

        return (outlier_string, info_dict)
