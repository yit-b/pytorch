import builtins
import collections
import dataclasses
import importlib
import itertools
import logging
import math
import os
import re
import types
import weakref
from inspect import currentframe, getframeinfo
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, Type, Union
from weakref import ReferenceType

import torch

from torch._guards import (
    DuplicateInputs,
    Guard,
    GuardBuilderBase,
    GuardEnvExpr,
    GuardSource,
    Source,
)
from torch.fx.experimental.symbolic_shapes import SYMPY_INTERP

from . import config, convert_frame, mutation_guard
from .eval_frame import set_guard_error_hook
from .exc import unimplemented
from .types import GuardedCode, GuardFail, GuardFn  # noqa: F401
from .utils import (
    dict_const_keys,
    dict_const_keys_repr,
    dict_param_key_ids,
    guard_failures,
    HAS_NUMPY,
    istype,
    np,
    tensor_always_has_static_shape,
    tuple_iterator_getitem,
    tuple_iterator_len,
)

log = logging.getLogger(__name__)
TensorGuards = torch._C._dynamo.guards.TensorGuards
check_obj_id = torch._C._dynamo.guards.check_obj_id
check_type_id = torch._C._dynamo.guards.check_type_id


CLOSURE_VARS = collections.OrderedDict(
    [
        ("___check_type_id", check_type_id),
        ("___check_obj_id", check_obj_id),
        ("___is_grad_enabled", torch.is_grad_enabled),
        (
            "___are_deterministic_algorithms_enabled",
            torch.are_deterministic_algorithms_enabled,
        ),
        ("___odict_getitem", collections.OrderedDict.__getitem__),
        ("___dict_param_key_ids", dict_param_key_ids),
        ("___dict_const_keys", dict_const_keys),
        ("___tuple_iterator_len", tuple_iterator_len),
        ("___tuple_iterator_getitem", tuple_iterator_getitem),
        ("__math_isnan", math.isnan),
        ("inf", float("inf")),
        ("__load_module", lambda name: importlib.import_module(name)),
    ]
)


def strip_function_call(name):
    """
    "___odict_getitem(a, 1)" => "a"
    "a.layers[slice(2)][0]._xyz" ==> "a"
    "getattr(a.layers[slice(2)][0]._abc, '0')" ==> "a"
    "getattr(getattr(a.x[3], '0'), '3')" ==> "a"
    "a.layers[slice(None, -1, None)][0]._xyz" ==> "a"
    """
    # recursively find valid object name in fuction
    valid_name = re.compile("[A-Za-z_].*")
    curr = ""
    for char in name:
        if char in " (":
            curr = ""
        elif char in "),[]":
            if curr and curr != "None" and valid_name.match(curr):
                return strip_function_call(curr)
        else:
            curr += char

    return strip_getattr_getitem(name)


def strip_getattr_getitem(name):
    """
    "a[1]" => "a"
    "a.foo" => "a"
    """
    return re.split(r"[.\[]", name)[0]


@dataclasses.dataclass
class CodePart:
    """
    A CodePart represents a code string and bookeeping information accumulated at guard creation time.
    CodeParts are used to make up the check_fn we use for guards. A collection of CodeParts is kept on each
    guard cache entry, and is passed into the subsequent eval_frame callback upon guard failure.
    """

    source: Optional[
        List[Source]
    ]  # Note: List of sources for tensor checks and shape_env
    code: str
    origin: str

    # bound at guard failure time
    scope = None

    # tensor check only
    check_tensor_verbose = None
    tensor_check_names: Optional[str] = None

    def __hash__(self):
        return hash(self.code)

    def __eq__(self, other):
        return self.code == other.code


class GuardBuilder(GuardBuilderBase):
    def __init__(
        self,
        id_ref: Callable[[Type[object]], str],
        source_ref: Callable[[Source], str],
        user_scope: Optional[Dict[str, object]],
        check_fn_manager: "CheckFunctionManager",
        *,
        local: bool,
    ):
        self.id_ref = id_ref
        self.source_ref = source_ref
        if user_scope:
            scope = {"L" if local else "G": user_scope}
        else:
            scope = {"L" if local else "G": dict()}
        self.scope: Dict[str, Dict[str, object]] = scope
        self.scope["__builtins__"] = builtins.__dict__.copy()
        for (
            name,
            package_module,
        ) in torch.package.package_importer._package_imported_modules.items():
            name = name.replace(">", "_").replace("<", "_").replace(".", "_dot_")
            # Write the package module into the scope so that we can import it
            self.scope["__builtins__"][name] = package_module  # type: ignore[index]
            # Write the demangled name to the scope so that we can use it
            self.scope[name] = package_module

        self.argnames: List[str] = []
        # Code is python expression strings generated for each guard
        self.code: List[CodePart] = []
        # shape_env_code is only used by local_builder and is used for
        # shape env code.  This exists only because we need to make sure
        # shape env guards get run after tensor match guards (since the
        # tensor match guards make sure we actually have tensors)
        self.shape_env_code: List[CodePart] = []

        # [Note - On Eager Tensor Guards]
        # Most of the time, we generate Python code in a guard to directly
        # check various properties.  However, tensors are a bit special;
        # it is too slow to check their properties one-by-one in Python.
        # Instead, there is a C++ function TensorGuards.check which takes
        # all of the tensor arguments and checks them all against compile-time
        # examples entirely in C++.  Thus, every time we process a
        # TENSOR_MATCH guard, we just add another entry to
        # tensor_check_names/tensor_check_examples, saying "for this local,
        # check it against this example", and it all ends up getting
        # swept up into a single call to ___check_tensors.  Invariant:
        # len(tensor_check_names) == len(tensor_check_examples).
        self.tensor_check_names: List[str] = []
        self.tensor_check_sources: List[Source] = []
        self.tensor_check_examples: List[torch.Tensor] = []

        self.check_fn_manager: CheckFunctionManager = check_fn_manager

    # Warning: use this with care!  This lets you access what the current
    # value of the value you are guarding on is.  You probably don't want
    # to actually durably save this value though (because it's specific
    # to this frame!)  Instead, you should be reading out some property
    # (like its type) which is what you permanently install into the
    # guard code.
    def get(self, name: str) -> Any:
        return eval(name, self.scope, CLOSURE_VARS)

    # Registers the usage of the source name referenced by the
    # string (or stored in the Guard) as being guarded upon.  It's important
    # to call this before generating some code that makes use of 'guard',
    # because without this call, we won't actually bind the variable
    # you reference in the actual guard closure (oops!)
    def arg_ref(self, guard: Union[str, Guard]) -> str:
        name: str
        if isinstance(guard, str):
            name = guard
        else:
            name = guard.name
        base = strip_getattr_getitem(strip_function_call(name))
        if base not in self.argnames:
            if re.match(r"[a-zA-Z0-9_]+", base):
                if re.match(r"^\d+$", base):
                    log.warning(f"invalid var name: {guard}")
                self.argnames.append(base)

        return name

    def TYPE_MATCH(self, guard: Guard):
        # ___check_type_id is same as `id(type(x)) == y`
        t = type(self.get(guard.name))
        obj_id = self.id_ref(t)
        code = f"___check_type_id({self.arg_ref(guard)}, {obj_id})"
        self._produce_guard_code(guard, [code])

    def BOOL_FALSE(self, guard: Guard):
        # Guard on the runtime value being 'False',
        # can be faster than seemingly equivalent checks like DICT_KEYS for empty dict
        #
        # WARNING: this guard is not safe to use generally.  It only works if the runtime
        # value is of a type that supports bool(), and some types e.g. Tensor do not.
        # Only use this guard in cases you can guarantee the runtime type will be friendly.
        # (e.g. Specialized NNModule with mutation protection via setattr)
        #
        # Why not simply check the runtime type inside this guard?  It's slow enough to defeat
        # the purpose of using this guard, which itself is supposed to be a faster alternative
        # to DICT_KEYS.
        ref = self.arg_ref(guard)
        code = f"not {ref}"
        self._produce_guard_code(guard, [code])

    def ID_MATCH(self, guard: Guard):
        # ___check_obj_id is same as `id(x) == y`
        m = re.match(r"^type\((.+)\)$", guard.name)
        if m:
            # optional optimization to produce cleaner/faster guard code
            return self.TYPE_MATCH(
                Guard(m.group(1), guard.source, GuardBuilder.TYPE_MATCH)
            )

        code = f"___check_obj_id({self.arg_ref(guard)}, {self.id_ref(self.get(guard.name))})"
        self._produce_guard_code(guard, [code])

    def NAME_MATCH(self, guard: Guard):
        obj = self.get(guard.name)
        code = f"{self.arg_ref(guard)}.__name__ == '{obj.__name__}'"
        self._produce_guard_code(guard, [code])

    def HASATTR(self, guard: Guard):
        m = re.match(r"^(.*)[.]([a-zA-Z0-9_]+)$", guard.name)
        assert m, f"invalid hasattr check {guard.name}"
        base, attr = m.group(1, 2)
        ref = self.arg_ref(base)
        val = hasattr(self.get(base), attr)
        code = None
        if val:
            code = f"hasattr({ref}, {attr!r})"
        else:
            code = f"not hasattr({ref}, {attr!r})"

        self._produce_guard_code(guard, [code], provided_guarded_object=self.get(base))

    def EQUALS_MATCH(self, guard: Guard):
        ref = self.arg_ref(guard)
        val = self.get(guard.name)
        t = type(val)
        np_types = (
            (
                np.int8,
                np.int16,
                np.int32,
                np.int64,
                np.uint8,
                np.uint16,
                np.uint32,
                np.uint64,
                np.float16,
                np.float32,
                np.float64,
            )
            if HAS_NUMPY
            else ()
        )
        ok_types = (
            int,
            float,
            bool,
            type(None),
            str,
            type,
            list,
            tuple,
            set,
            slice,
            frozenset,
            range,
            torch.Size,
            torch.device,
            torch.dtype,
            *np_types,
        )
        if istype(val, dict):
            assert all(
                istype(x, ok_types) for x in itertools.chain(val.keys(), val.values())
            )
        else:
            assert istype(
                val,
                ok_types,
            ), t.__name__

        if istype(val, (torch.device, torch.dtype)):
            # TODO(jansel): is this slow? perhaps optimize it
            code = [f"str({ref}) == {str(val)!r}"]
            self._produce_guard_code(guard, code)
            return

        # Special case for nan because float("nan") == float("nan") evaluates to False
        if istype(val, float) and math.isnan(val):
            code = list()
            code.append(f"___check_type_id({ref}, {self.id_ref(t)})")
            code.append(f"__math_isnan({ref})")
            self._produce_guard_code(guard, code)
            return

        code = list()

        # If matching equality against list/tuple, we must also check that
        # the internal types match.  (TODO: what about nested lists?)
        if istype(val, (list, tuple)):
            # NB: LIST_LENGTH takes care of the outer __check_type_id test
            self.LIST_LENGTH(guard)

            for idx, elem in enumerate(val):
                code.append(
                    f"___check_type_id({ref}[{idx}], {self.id_ref(type(elem))})"
                )
        else:
            # Add type check to prevent equality check between tensor and non-tensor.
            code.append(f"___check_type_id({ref}, {self.id_ref(t)})")

        if istype(val, torch.Size):
            val = tuple(val)

        # TODO: It feels like it would be better to just implement our own
        # equality test in C that handles all of the necessary type checking
        # and NaN tests
        code.append(f"{ref} == {val!r}")
        self._produce_guard_code(guard, code)

    def CONSTANT_MATCH(self, guard: Guard):
        val = self.get(guard.name)
        if istype(val, (bool, type(None))):
            self.ID_MATCH(guard)
        else:
            self.EQUALS_MATCH(guard)

    def NN_MODULE(self, guard: Guard):
        self.ID_MATCH(guard)
        ref = self.arg_ref(guard)
        val = self.get(guard.name)

        if hasattr(val, "training"):
            # There are cases where a monkeypatched object has a guard made between __new__ and __init__
            assert istype(val.training, bool)
            self._produce_guard_code(guard, [f"{ref}.training == {val.training}"])
        else:
            unimplemented(f"Guard setup for uninitialized class {type(val)}")

    def FUNCTION_MATCH(self, guard: Guard):
        """things like torch.add and user defined functions"""
        if guard.is_local():
            return self.ID_MATCH(guard)

    def BUILTIN_MATCH(self, guard: Guard):
        return self.FUNCTION_MATCH(guard)

    def PYMODULE_MATCH(self, guard: Guard):
        return self.FUNCTION_MATCH(guard)

    def LIST_LENGTH(self, guard):
        ref = self.arg_ref(guard)
        value = self.get(guard.name)
        t = type(value)

        code = list()
        code.append(f"___check_type_id({ref}, {self.id_ref(t)})")
        code.append(f"len({ref}) == {len(value)}")

        self._produce_guard_code(guard, code)

    def TUPLE_ITERATOR_LEN(self, guard):
        ref = self.arg_ref(guard)
        value = self.get(guard.name)
        t = type(value)

        code = list()
        code.append(f"___check_type_id({ref}, {self.id_ref(t)})")
        code.append(f"___tuple_iterator_len({ref}) == {tuple_iterator_len(value)}")

        self._produce_guard_code(guard, code)

    def DICT_KEYS(self, guard):
        ref = self.arg_ref(guard)
        value = self.get(guard.name)
        t = type(value)

        code = list()
        code.append(f"___check_type_id({ref}, {self.id_ref(t)})")
        param_key_ids = set(dict_param_key_ids(value))
        const_keys = set(dict_const_keys(value))
        const_keys_repr = dict_const_keys_repr(const_keys)
        if param_key_ids:
            code.append(f"___dict_param_key_ids({ref}) == {param_key_ids!r}")
            code.append(f"___dict_const_keys({ref}) == {const_keys_repr}")
        else:
            code.append(f"set({ref}.keys()) == {const_keys_repr}")

        self._produce_guard_code(guard, code)

    def WEAKREF_ALIVE(self, guard):
        self._produce_guard_code(guard, [f"{self.arg_ref(guard)} is not None"])

    def NN_MODULE_PARAM_NAMES(self, guard):
        ref = self.arg_ref(guard)
        value = self.get(guard.name)
        t = type(value)
        keys = {k for k, v in value.named_parameters()}

        code = list()
        code.append(f"___check_type_id({ref}, {self.id_ref(t)})")
        code.append(f"{{k for k, v in {ref}.named_parameters()}} == {keys!r}")

        self._produce_guard_code(guard, code)

    def ODICT_KEYS(self, guard):
        """OrderedDict keys match"""
        ref = self.arg_ref(guard)
        value = self.get(guard.name)
        t = type(value)

        code = list()
        code.append(f"___check_type_id({ref}, {self.id_ref(t)})")
        code.append(f"str({ref}.keys()) == {str(value.keys())!r}")

        self._produce_guard_code(guard, code)

    def OBJECT_MUTATION(self, guard: Guard):
        mutation_guard.watch(self.get(guard.name), self.check_fn_manager)

    def GRAD_MODE(self, guard: Guard):
        """Guard on the initial grad state"""
        assert guard.name == ""
        assert guard.source is GuardSource.GLOBAL
        code = None
        if convert_frame.initial_grad_state:
            code = "___is_grad_enabled()"
        else:
            code = "not ___is_grad_enabled()"
        self._produce_guard_code(guard, [code])

    def DETERMINISTIC_ALGORITHMS(self, guard: Guard):
        """Guard on the initial determinism algorithms state"""
        assert guard.source is GuardSource.GLOBAL
        code = None
        if convert_frame.initial_deterministic_algorithms_state:
            code = "___are_deterministic_algorithms_enabled()"
        else:
            code = "not ___are_deterministic_algorithms_enabled()"
        self._produce_guard_code(guard, [code])

    def SHAPE_ENV(self, guard: Guard):
        # Let's handle ShapeEnv guards.  To do this, we will resolve
        # shape variables to sources from tracked_fakes.  This must happen after
        # tensor checks.
        assert guard.name == ""
        output_graph = self.check_fn_manager.output_graph
        # NB: self.output_graph can be None in the debug_nops tests
        fs = output_graph.tracked_fakes
        constraint_inputs = [a.constraint_dims for a in fs]
        guards = output_graph.shape_env.produce_guards(
            [a.fake for a in fs],
            [a.source for a in fs],
            constraint_inputs=constraint_inputs,
            source_ref=self.source_ref,
        )
        origin = guard.origin
        for shape_guard in guards:
            self._produce_guard_code(
                guard, [shape_guard.expr], shape_env=True, sources=shape_guard.sources
            )

    def TENSOR_MATCH(self, guard: Guard):
        if guard.is_nn_module():
            self.ID_MATCH(guard)
        else:
            value = self.get(guard.name)
            assert isinstance(value, torch.Tensor)
            tensor_name = self.arg_ref(guard)
            # [Note - On Export Tensor Guards]
            #
            # In eager mode, tensor guards are evaluated through C++, in guards.cpp
            # see [Note - On Eager Tensor Guards] for more info.
            #
            # In export mode, we instead maintain parallel logic between C++ and python
            # here, with an exception of checking the dispatch key - with the idea that a dispatch key
            # is an entirely runtime notion that would make no sense to keep in an exported graph.
            #
            # Now, this idea is okay, but to paraphrase @ezyang, this mental model is sufficient for now, although
            # not entirely true.
            # For example, suppose one of the input tensors had the negative dispatch key.
            # You should end up with a graph that is specialized for tensors that have a negative dispatch key.
            # If you allow a Tensor that does NOT have this bit set, you will accidentally run it "as if" it were negated.
            # Now, negative key only shows up for complex numbers, and most likely, the exported to target doesn't
            # support this feature at all, but the point stands that :some: tensor state only shows up on dispatch key.
            # TODO(voz): Either populate a dispatch_key check into the guards, or error on users passing in an unsupported
            # subset of keys during export.
            #
            # The list of tensor fields and calls we care about can be found in `terms` below.
            # TODO(voz): We are missing storage offset in all our tensor guards?
            code: List[str] = list()
            if self.check_fn_manager.output_graph.export:
                self.TYPE_MATCH(guard)
                terms = [
                    "dtype",
                    "device.type",
                    "device.index",
                    "requires_grad",
                    "ndimension()",
                ]
                if not config.dynamic_shapes:
                    terms.append("stride()")
                    # We need to do this to avoid the torch.Size type in guards
                    code.append(f"{tensor_name}.shape == {tuple(value.shape)}")

                for term in terms:
                    real_value = self.get(tensor_name + "." + term)
                    code.append(f"{tensor_name}.{term} == {real_value}")
            else:
                self.tensor_check_names.append(tensor_name)
                self.tensor_check_sources.append(guard.origin)
                self.tensor_check_examples.append(value)

            # A frame is valid for reuse with dynamic dimensions if the new dynamic dimensions are a
            # strict subset of the old.
            #
            # The logic here is as follows:
            #
            # Every mark_dynamic directive is a user-knows-best command, which can incur a raise at tracing
            # time if we find guards that run counter to the user directive.
            # If compiling a frame with explicit dynamic dims X could cause an exception, we MUST NOT skip compiling.
            #
            # If the frame is compiled with any marked dynamic indices, let's call that set of indices X.
            # When we evaluated inputs against the guards, given the same tensor with potentially new dynamic indices,
            # let's call that set Y.
            #
            # When X is a strict subset of Y, the potential new raises introduced during compilation are a strict subset
            # of the raises we
            # could have encountered. The frame compiled under Y is safe to reuse with X.
            # When X is not a strict subset of Y, the non-overlapping new elements of X may cause new raises, and the
            # frame is no longer fit for reuse.
            #
            # This is the case because any newly introduced mark_dynamic directives have a chance of
            # raising, failing compilation. Any existing mark_dynamic indices that we lost are safe to lose
            # as all it means is that we have gotten rid of a user directive which could incur a raise at compile time.
            # In the case of when there is no Y, that is, there are no dynamic indices marked at all, the frame is safe
            # to reuse
            # as an empty set is a safe degeneration - that is, a strictly static tensor is always valid for a frame
            # compiled with that same
            # tensor + more onerous user directives.
            assert guard.source is not None
            static, reason = tensor_always_has_static_shape(value, is_tensor=True)
            if not static:
                if hasattr(value, "_dynamo_dynamic_indices"):
                    code.append(
                        f"({tensor_name}._dynamo_dynamic_indices.issubset({value._dynamo_dynamic_indices})) if hasattr({tensor_name}, '_dynamo_dynamic_indices') else True"  # noqa: B950
                    )
                # In the case of us not having any dynamic dimension indices, we compiled the frame with no chance of
                # raising for this specific tensor - and any inputs with more dynamic user directives specified must be recompiled.
                else:
                    code.append(
                        f"hasattr({tensor_name}, '_dynamo_dynamic_indices') == False"
                    )

            if len(code) > 0:
                self._produce_guard_code(guard, code)

    # A util that appends guarded code, or, in the case of export, adds data onto guards
    def _produce_guard_code(
        self,
        guard,
        code_list,
        provided_guarded_object=None,
        shape_env=False,
        sources=None,
    ):
        # WARNING: It is important that cur_frame/caller do NOT stay in
        # the current frame, because they will keep things live longer
        # than they should.  See TestMisc.test_release_module_memory
        cur_frame = currentframe()
        assert cur_frame is not None
        caller = cur_frame.f_back
        del cur_frame
        assert caller is not None
        func_name = getframeinfo(caller)[2]
        # We use func_name for export, so might as well get a nice defensive check out of it
        assert func_name in dir(
            self.__class__
        ), f"_produce_guard_code must be called from inside GuardedCode. Called from {func_name}"

        caller_fn = getframeinfo(caller)[2]
        del caller
        for code in code_list:
            if sources is None:
                sources = [guard.origin]
            code_part = CodePart(sources, code, caller_fn)
            if shape_env:
                self.shape_env_code.append(code_part)
            else:
                self.code.append(code_part)

        # Not all guards have names, some can be installed globally (see asserts on HAS_GRAD)
        if provided_guarded_object is None:
            name_valid = guard.name is not None and guard.name != ""

            guarded_object = self.get(guard.name) if name_valid else None
        else:
            guarded_object = provided_guarded_object

        guarded_object_type = (
            weakref.ref(type(guarded_object)) if guarded_object is not None else None
        )
        obj_ref = None
        if hasattr(guarded_object.__class__, "__weakref__"):
            obj_ref = weakref.ref(guarded_object)

        guard.set_export_info(
            func_name,
            guarded_object_type,
            code_list,
            obj_ref,
        )


# NB: Naively, you'd expect this to only be a function that produces
# the callable that constitutes the guard.  However, there is some
# delicate handling for invalidating this check function when the
# locals/globals get invalidated, so there's some extra state
# we have to hold in this manager class.
#
# TODO: this object has reference cycle with itself, via check_fn which
# references back to CheckFunction via ___guarded_code in closure_vars.
# Ideally, there shouldn't be any ref cycle so that guards are
# promptly disposed of.
class CheckFunctionManager:
    def __init__(
        self,
        output_graph=None,
        f_locals: Optional[Dict[str, object]] = None,
        f_globals: Optional[Dict[str, object]] = None,
        guard_fail_fn: Optional[Callable[[Tuple[str, str]], None]] = None,
    ):
        guards = output_graph.guards if output_graph else None
        self.valid = True
        self._weakrefs: List["ReferenceType[object]"] = []
        self._seen_ids: Set[int] = set()
        self.output_graph = output_graph

        # Note: right overrides left
        def combine_scopes(left, right):
            if left is None:
                return right

            if right is None:
                return left

            return {**left, **right}

        def source_ref(source):
            guard_source = source.guard_source()
            if guard_source is GuardSource.CONSTANT:
                # No need to track constants
                return source.name()
            builder = guard_source.select(w_local(), w_global())
            assert builder is not None
            return builder.arg_ref(source.name())

        local_builder = GuardBuilder(
            self.id_ref,
            source_ref,
            combine_scopes(f_globals, f_locals),
            self,
            local=True,
        )
        global_builder = GuardBuilder(
            self.id_ref, source_ref, f_globals, self, local=False
        )
        # We need to transplant a copy here, because some guards
        # might get a cross ref between local and global, like L['mod_name'][G['some_key']]
        # the inverse is illegal.
        if "G" in global_builder.scope:
            local_builder.scope["G"] = global_builder.scope["G"]
        # source_ref can cause a cycle, make sure we break it with weakref
        w_local = weakref.ref(local_builder)
        w_global = weakref.ref(global_builder)
        for guard in sorted(guards or [], key=Guard.sort_key):
            if (
                not config.guard_nn_modules
                and guard.is_nn_module()
                # Default func args must be guarded on.
                # TODO: we could make use of 'DefaultsSource' and offer a .guard.is_defaults() API
                and "__defaults__" not in guard.name
                and "__kwdefaults__" not in guard.name
                and (config.skip_nnmodule_hook_guards or "hooks" not in guard.name)
            ):
                continue
            guard.create(local_builder, global_builder)
        self.check_fn = self.compile_check_fn(
            local_builder, global_builder, guards, guard_fail_fn
        )
        self._seen_ids.clear()

    def compile_check_fn(
        self, local_builder, global_builder, guards_out, guard_fail_fn
    ):
        assert not (set(local_builder.argnames) & set(global_builder.argnames))
        # see parallel handling of ".0" / "___implicit0" in _eval_frame.c
        largs = local_builder.argnames
        largs += ["**___kwargs_ignored"]
        args = ",".join(largs)

        validation_code_part = CodePart(None, "___guarded_code.valid", "")
        code_parts = []
        code_parts.append(validation_code_part)
        code_parts += local_builder.code + global_builder.code
        part_list: List[CodePart] = []

        tensor_check_names = (
            local_builder.tensor_check_names + global_builder.tensor_check_names
        )
        tensor_check_sources = (
            local_builder.tensor_check_sources + global_builder.tensor_check_sources
        )
        assert len(tensor_check_names) == len(tensor_check_sources)

        check_tensors_fn = None
        check_tensor_verbose = None
        if tensor_check_names:
            assert (
                not self.output_graph.export
            ), "Illegal to set tensor_check_names in export."
            tensor_check_examples = (
                local_builder.tensor_check_examples
                + global_builder.tensor_check_examples
            )
            tensor_guards = TensorGuards(
                *tensor_check_examples, dynamic_shapes=config.dynamic_shapes
            )
            check_tensors_fn = tensor_guards.check
            check_tensor_names = ", ".join(tensor_check_names)
            check_tensor_slug = f"___check_tensors({check_tensor_names})"
            code_part = CodePart(
                tensor_check_sources, check_tensor_slug, "TENSOR_MATCH"
            )
            check_tensor_verbose = tensor_guards.check_verbose
            code_part.tensor_check_names = tensor_check_names
            code_parts.append(code_part)

        aotautograd_guards: List[GuardEnvExpr] = (
            self.output_graph.tracing_context.guards_context.aotautograd_guards
            if self.output_graph
            else []
        )
        for guard in aotautograd_guards:
            if isinstance(guard, DuplicateInputs):
                source_a = guard.input_source_a
                source_b = guard.input_source_b
                code_part = CodePart(
                    [source_a, source_b],
                    f"{source_a.name()} is {source_b.name()}",
                    "DuplicateInputs",
                )
                code_parts.append(code_part)
            else:
                raise RuntimeError(f"Unknown GuardEnvExpr: {guard}")

        code_parts.extend(local_builder.shape_env_code)
        assert not global_builder.shape_env_code

        closure_vars = collections.OrderedDict(
            [
                ("___guarded_code", self),
                ("___check_tensors", check_tensors_fn),
                ("___check_tensors_verbose", check_tensor_verbose),
                ("tensor_check_names", tensor_check_names),
                ("part_list", part_list),
            ]
            + list(SYMPY_INTERP.items())
        )
        closure_vars.update(CLOSURE_VARS)

        # Let's go over how this code works.
        #
        # 1) The cache entry structure in eval_frame.c is stored in the frame's extra field. Each cache_entry is a node
        # in a linked list. Each cache entry represents a compiled frame with specializations. In order to know if
        # a cache entry's compiled frame is valid for reuse, we need to invoke a function, check_fn,
        # to compare current state against the specializations captured at compile time.
        #
        # 2) The function, check_fn, mentioned above, is defined by executing the function below, ___make_guard_fn
        #
        # 3) In this, this code is rather confusing, because it defines both ___make_guard_fn and the lambda it produces
        # which becomes the check_fn.
        #
        # 4) Everything in `code` becomes the check_fn. We write it out by first defining a `__fail`, which
        # given a code_part_id, the id() of a code_part, extract a code_part from the part_list and binds a scope
        # to it. This is used later for evaluating the expression defined in code_part, if necessary. `__fail` is
        # invoked if a specific sub expression of a guard is failed.
        #
        # 5) The sub expressions of guards are defined 1 per code_part. We iterate over the code_parts and produce
        # code that (a) assigns the result of the expression to a variable named `passing` (b) checks if not passing,
        # (c) and if not passing, returns __fail(code_part_id) with the id of the code_part used to produce the code.
        # or (d) if passing, proceeds until we've run all the sub expressions through.
        #
        # 6) In the event that we re-enter frame evaluation having failed a guard, we return the code_part
        # and pass it through to the frame evaluation callback. This is where downstream systems that handle guard
        # failures hook in, like guard failure logging, or converting static shape failures to dynamic shapes (if
        # the config is set).
        py_code = make_guard_fn(code_parts, closure_vars, part_list)
        if os.environ.get("TORCHDYNAMO_PRINT_GUARDS", None) == "1":
            print("GUARDS", code)
        out: Dict[str, Any] = dict()
        exec(py_code, global_builder.scope, out)
        guard_fn = out["___make_guard_fn"](*closure_vars.values())
        guard_fn.closure_vars = closure_vars
        # TODO(whc) maybe '.code_parts' was only kept around for the guard callback? so we don't need both
        guard_fn.args = largs
        guard_fn.code_parts = code_parts
        # Grab only G, but preserve "G" because guards access it as "G"
        guard_fn.global_scope = {"G": global_builder.scope["G"]}
        guard_fn.guard_fail_fn = guard_fail_fn
        guard_fn.part_list = part_list
        return guard_fn

    def invalidate(self, ref):
        # A weakref is no longer valid, self.check_fn should return false
        self.valid = False

    def id_ref(self, obj):
        """add a weakref, return the id"""
        try:
            if id(obj) not in self._seen_ids:
                self._weakrefs.append(weakref.ref(obj, self.invalidate))
                self._seen_ids.add(id(obj))
        except TypeError:
            pass  # cannot weakref bool object
        return id(obj)


def record_guard_failure(
    guard_fail_fn,
    code,
    code_part: CodePart,
) -> str:
    """
    called whenever a guard fails.
    """
    reason = code_part.code
    if "__check_tensors" in reason:
        assert code_part.tensor_check_names is not None
        reason = eval(
            f"___check_tensors_verbose({', '.join(code_part.tensor_check_names)}, tensor_check_names={code_part.tensor_check_names})",  # noqa: B950
            code_part.scope,
        )
    guard_failures[code].append(reason)
    if guard_fail_fn:
        guard_fail_fn(GuardFail(reason, code))

    return reason


# TODO(voz): Rewrite this API, we don't use most of these,
# leftover from when we had 2 fns.
def guard_error_hook(
    guard_fn: GuardFn,
    code: types.CodeType,
    f_locals: Dict[str, object],
    index: int,
    last: bool,
):
    print(
        f"ERROR RUNNING GUARDS {code.co_name} {code.co_filename}:{code.co_firstlineno}"
    )
    # TODO: If we passed in the exception here, we could get a precise
    # column number of which subexpression failed.  But that would also
    # require us to have the TRUE code that was eval'ed, not a shoddy
    # reconstruction (like is done here)
    print(make_guard_fn(guard_fn.code_parts, {}, []))


set_guard_error_hook(guard_error_hook)


def make_guard_fn(code_parts, closure_vars, part_list):
    # TODO(voz): Move this somewhere more general so we don't have to violate heirarchy?
    from torch._inductor.utils import IndentedBuffer

    make_fail_buf = IndentedBuffer()
    make_fail_buf.writeline("def __fail(code_part_idx):")
    with make_fail_buf.indent():
        make_fail_buf.writeline("code_part = part_list[code_part_idx]")
        make_fail_buf.writeline("code_part.scope = locals()")
        make_fail_buf.writeline("code_part.scope['L'] = L")
        for key, value in closure_vars.items():
            if callable(value):
                make_fail_buf.writeline(f"code_part.scope['{key}'] = {key}")
        make_fail_buf.writeline("return code_part")

    assert len(part_list) == 0

    code_buf = IndentedBuffer()
    code_buf.writeline("passing = True")
    unique_code_parts = unique(code_parts)
    for i, code_part in enumerate(unique_code_parts):
        part_list.append(code_part)
        code_buf.writeline(f"passing = {code_part.code}")
        code_buf.writeline("if not passing:")
        with code_buf.indent():
            code_buf.writeline(f"return (False, __fail({i}))")

    code_buf.writeline("return (True, None)")

    make_guard_fn_buf = IndentedBuffer()
    make_guard_fn_buf.writeline(
        f"def ___make_guard_fn({','.join(closure_vars.keys())}):"
    )
    with make_guard_fn_buf.indent():
        make_guard_fn_buf.writeline("def guard_fn(L):")
        with make_guard_fn_buf.indent():
            make_guard_fn_buf.splice(make_fail_buf)
            make_guard_fn_buf.splice(code_buf)
        make_guard_fn_buf.writeline("return guard_fn")

    return make_guard_fn_buf.getvalue()


def unique(seq):
    seen = set()
    for x in seq:
        if x not in seen:
            yield x
            seen.add(x)
