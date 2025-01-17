#
# This source file is part of the EdgeDB open source project.
#
# Copyright 2008-present MagicStack Inc. and the EdgeDB authors.
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
#


"""Static evaluation of EdgeQL IR."""


from __future__ import annotations

import dataclasses
import decimal
import functools
from typing import *  # NoQA
import uuid

from edb import errors

from edb.edgeql import ast as qlast
from edb.edgeql import compiler as ql_compiler
from edb.edgeql import qltypes
from edb.edgeql.parser.grammar import lexutils as ql_lexutils

from edb.ir import ast as irast
from edb.ir import utils as irutils

from edb.schema import types as s_types
from edb.schema import schema as s_schema

from edb.server import config


class StaticEvaluationError(errors.QueryError):
    pass


class UnsupportedExpressionError(errors.QueryError):
    pass


def evaluate_to_python_val(
        ir: irast.Base,
        schema: s_schema.Schema) -> object:
    const = evaluate(ir, schema=schema)
    return const_to_python(const, schema=schema)


@functools.singledispatch
def evaluate(
        ir: irast.Base,
        schema: s_schema.Schema) -> irast.BaseConstant:
    raise UnsupportedExpressionError(
        f'no static IR evaluation handler for {ir.__class__}')


@evaluate.register(irast.SelectStmt)
def evaluate_SelectStmt(
        ir_stmt: irast.SelectStmt,
        schema: s_schema.Schema) -> irast.BaseConstant:

    if irutils.is_trivial_select(ir_stmt):
        return evaluate(ir_stmt.result, schema)
    else:
        raise UnsupportedExpressionError(
            'expression is not constant', context=ir_stmt.context)


@evaluate.register(irast.Set)
def evaluate_Set(
        ir_set: irast.Set,
        schema: s_schema.Schema) -> irast.BaseConstant:
    if ir_set.expr is not None:
        return evaluate(ir_set.expr, schema=schema)
    else:
        raise UnsupportedExpressionError(
            'expression is not constant', context=ir_set.context)


@evaluate.register(irast.ConstExpr)
def evaluate_BaseConstant(
        ir_const: irast.ConstExpr,
        schema: s_schema.Schema) -> irast.ConstExpr:
    return ir_const


op_table = {
    # Concatenation
    ('INFIX', 'std::++'): lambda a, b: a + b,
}


@evaluate.register(irast.OperatorCall)
def evaluate_OperatorCall(
        opcall: irast.OperatorCall,
        schema: s_schema.Schema) -> irast.BaseConstant:

    if irutils.is_union_expr(opcall):
        return _evaluate_union(opcall, schema)

    eval_func = op_table.get((opcall.operator_kind, opcall.func_shortname))
    if eval_func is None:
        raise UnsupportedExpressionError(
            f'unsupported operator: {opcall.func_shortname}',
            context=opcall.context)

    args = []
    for arg in opcall.args:
        arg_val = evaluate_to_python_val(arg.expr, schema=schema)
        if isinstance(arg_val, tuple):
            raise UnsupportedExpressionError(
                f'non-singleton operations are not supported',
                context=opcall.context)

        args.append(arg_val)

    value = eval_func(*args)
    qlconst = qlast.BaseConstant.from_python(value)

    result = ql_compiler.compile_constant_tree_to_ir(
        qlconst, styperef=opcall.typeref, schema=schema)

    return result


def _evaluate_union(
        opcall: irast.OperatorCall,
        schema: s_schema.Schema) -> irast.ConstantSet:

    elements = []
    for arg in opcall.args:
        val = evaluate(arg.expr, schema=schema)
        if isinstance(val, irast.ConstantSet):
            elements.extend(val.elements)
        else:
            elements.append(val)

    return irast.ConstantSet(
        elements=tuple(elements),
        typeref=next(iter(elements)).typeref,
    )


@functools.singledispatch
def const_to_python(
        ir: irast.BaseConstant,
        schema: s_schema.Schema) -> object:
    raise UnsupportedExpressionError(
        f'cannot convert {ir!r} to Python value')


@const_to_python.register(irast.ConstantSet)
def const_set_to_python(
        ir: irast.ConstantSet,
        schema: s_schema.Schema) -> tuple:
    return tuple(const_to_python(v, schema) for v in ir.elements)


@const_to_python.register(irast.IntegerConstant)
def int_const_to_python(
        ir: irast.IntegerConstant,
        schema: s_schema.Schema) -> object:

    stype = schema.get_by_id(ir.typeref.id)
    if stype.issubclass(schema, schema.get('std::decimal')):
        return decimal.Decimal(ir.value)
    else:
        return int(ir.value)


@const_to_python.register(irast.FloatConstant)
def float_const_to_python(
        ir: irast.FloatConstant,
        schema: s_schema.Schema) -> object:

    stype = schema.get_by_id(ir.typeref.id)
    if stype.issubclass(schema, schema.get('std::decimal')):
        return decimal.Decimal(ir.value)
    else:
        return float(ir.value)


@const_to_python.register(irast.StringConstant)
def str_const_to_python(
        ir: irast.StringConstant,
        schema: s_schema.Schema) -> str:

    return ql_lexutils.unescape_string(ir.value)


@const_to_python.register(irast.RawStringConstant)
def raw_str_const_to_python(
        ir: irast.RawStringConstant,
        schema: s_schema.Schema) -> str:

    return ir.value


@const_to_python.register(irast.BooleanConstant)
def bool_const_to_python(
        ir: irast.BooleanConstant,
        schema: s_schema.Schema) -> bool:

    return ir.value == 'true'


def schema_type_to_python_type(
        stype: s_types.Type,
        schema: s_schema.Schema) -> type:
    if stype.is_scalar():
        return scalar_type_to_python_type(stype, schema)
    elif stype.is_object_type():
        return object_type_to_python_type(stype, schema)
    else:
        raise UnsupportedExpressionError(
            f'{stype.get_displayname(schema)} is not representable in Python')


def scalar_type_to_python_type(
        stype: s_types.Type,
        schema: s_schema.Schema) -> type:

    typemap = {
        'std::str': str,
        'std::anyint': int,
        'std::anyfloat': float,
        'std::decimal': decimal.Decimal,
        'std::bool': bool,
        'std::json': str,
        'std::uuid': uuid.UUID,
    }

    for basetype, python_type in typemap.items():
        if stype.issubclass(schema, schema.get(basetype)):
            return python_type

    raise UnsupportedExpressionError(
        f'{stype.get_displayname(schema)} is not representable in Python')


def object_type_to_python_type(
        objtype: s_types.Type,
        schema: s_schema.Schema, *,
        base_class: Optional[type] = None,
        _memo: Optional[Mapping[s_types.Type, type]]=None
) -> type:

    if _memo is None:
        _memo = {}

    fields = []
    subclasses = []

    for pn, p in objtype.get_pointers(schema).items(schema):
        if pn in ('id', '__type__'):
            continue

        ptype = p.get_target(schema)

        if ptype.is_object_type():
            pytype = _memo.get(ptype)
            if pytype is None:
                pytype = object_type_to_python_type(
                    ptype, schema, base_class=base_class, _memo=_memo)
                _memo[ptype] = pytype

                for subtype in ptype.children(schema):
                    subclasses.append(
                        object_type_to_python_type(
                            subtype, schema,
                            base_class=pytype, _memo=_memo))
        else:
            pytype = scalar_type_to_python_type(ptype, schema)

        is_multi = p.get_cardinality(schema) is qltypes.Cardinality.MANY
        if is_multi:
            pytype = FrozenSet[pytype]

        default = p.get_default(schema)
        if default is None:
            if p.get_required(schema):
                default = dataclasses.MISSING
        else:
            default = ql_compiler.evaluate_to_python_val(
                default.text, schema=schema)
            if is_multi and not isinstance(default, frozenset):
                default = frozenset((default,))

        constraints = p.get_constraints(schema).objects(schema)
        exclusive = schema.get('std::exclusive')
        unique = (
            not ptype.is_object_type()
            and any(c.issubclass(schema, exclusive) for c in constraints)
        )
        field = dataclasses.field(
            compare=unique,
            hash=unique,
            repr=True,
            default=default,
        )
        fields.append((pn, pytype, field))

    return dataclasses.make_dataclass(
        objtype.get_name(schema).name,
        fields=fields,
        bases=(base_class,) if base_class is not None else (),
        frozen=True,
        namespace={'_subclasses': subclasses},
    )


@functools.singledispatch
def evaluate_to_config_op(
        ir: irast.Base,
        schema: s_schema.Schema) -> irast.BaseConstant:
    raise UnsupportedExpressionError(
        f'no config op evaluation handler for {ir.__class__}')


@evaluate_to_config_op.register
def evaluate_config_set(
        ir: irast.ConfigSet,
        schema: s_schema.Schema):

    return config.Operation(
        opcode=config.OpCode.CONFIG_SET,
        level=config.OpLevel.SYSTEM if ir.system else config.OpLevel.SESSION,
        setting_name=ir.name,
        value=evaluate_to_python_val(ir.expr, schema),
    )


@evaluate_to_config_op.register
def evaluate_config_reset(
        ir: irast.ConfigReset,
        schema: s_schema.Schema):

    return config.Operation(
        opcode=config.OpCode.CONFIG_RESET,
        level=config.OpLevel.SYSTEM if ir.system else config.OpLevel.SESSION,
        setting_name=ir.name,
        value=None,
    )
