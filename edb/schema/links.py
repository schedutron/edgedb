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


from __future__ import annotations

from typing import *  # NoQA

from edb.edgeql import ast as qlast
from edb.edgeql import qltypes

from edb import errors

from . import abc as s_abc
from . import constraints
from . import delta as sd
from . import indexes
from . import inheriting
from . import lproperties
from . import name as sn
from . import objects as so
from . import pointers
from . import referencing
from . import sources
from . import utils


LinkTargetDeleteAction = qlast.LinkTargetDeleteAction


def merge_actions(target: so.Object, sources: List[so.Object],
                  field_name: str, *, schema) -> object:
    ours = target.get_explicit_local_field_value(schema, field_name, None)
    if ours is None:
        current = None
        current_from = None

        for source in sources:
            theirs = source.get_explicit_field_value(schema, field_name, None)
            if theirs is not None:
                if current is None:
                    current = theirs
                    current_from = source
                elif current != theirs:
                    target_source = target.get_source(schema)
                    current_from_source = current_from.get_source(schema)
                    source_source = source.get_source(schema)

                    tgt_repr = (
                        f'{target_source.get_displayname(schema)}.'
                        f'{target.get_displayname(schema)}'
                    )
                    cf_repr = (
                        f'{current_from_source.get_displayname(schema)}.'
                        f'{current_from.get_displayname(schema)}'
                    )
                    other_repr = (
                        f'{source_source.get_displayname(schema)}.'
                        f'{source.get_displayname(schema)}'
                    )

                    raise errors.SchemaError(
                        f'cannot implicitly resolve the '
                        f'`on target delete` action for '
                        f'{tgt_repr!r}: it is defined as {current} in '
                        f'{cf_repr!r} and as {theirs} in {other_repr!r}; '
                        f'to resolve, declare `on target delete` '
                        f'explicitly on {tgt_repr!r}'
                    )
        return current
    else:
        return ours


class Link(sources.Source, pointers.Pointer, s_abc.Link):

    on_target_delete = so.SchemaField(
        LinkTargetDeleteAction,
        default=LinkTargetDeleteAction.RESTRICT,
        coerce=True,
        compcoef=0.9,
        merge_fn=merge_actions)

    def is_link_property(self, schema):
        return False

    def is_property(self, schema):
        return False

    def scalar(self):
        return False

    def has_user_defined_properties(self, schema):
        return bool([p for p in self.get_pointers(schema).objects(schema)
                     if not p.is_special_pointer(schema)])

    def compare(self, other, *, our_schema, their_schema, context=None):
        if not isinstance(other, Link):
            if isinstance(other, pointers.Pointer):
                return 0.0
            else:
                return NotImplemented

        return super().compare(
            other, our_schema=our_schema,
            their_schema=their_schema, context=context)

    def set_target(self, schema, target):
        schema = super().set_target(schema, target)
        tgt_prop = self.getptr(schema, 'target')
        schema = tgt_prop.set_target(schema, target)
        return schema

    @classmethod
    def get_root_classes(cls):
        return (
            sn.Name(module='std', name='link'),
            sn.Name(module='schema', name='__type__'),
        )

    @classmethod
    def get_default_base_name(self):
        return sn.Name('std::link')


class DerivedLink(pointers.Pointer, sources.Source):
    pass


class LinkSourceCommandContext(sources.SourceCommandContext):
    pass


class LinkSourceCommand(inheriting.InheritingObjectCommand):
    pass


class LinkCommandContext(pointers.PointerCommandContext,
                         constraints.ConsistencySubjectCommandContext,
                         lproperties.PropertySourceContext,
                         indexes.IndexSourceCommandContext):
    pass


class LinkCommand(lproperties.PropertySourceCommand,
                  pointers.PointerCommand,
                  schema_metaclass=Link, context_class=LinkCommandContext,
                  referrer_context_class=LinkSourceCommandContext):

    @classmethod
    def _process_link_create_or_alter(cls, schema, astnode, context, cmd):
        from . import objtypes as s_objtypes

        parent_ctx = context.get(LinkSourceCommandContext)

        if isinstance(astnode, qlast.CreateConcreteLink):
            # "source" attribute is set automatically as a refdict back-attr
            source_name = parent_ctx.op.classname

            if astnode.is_required is not None:
                cmd.set_attribute_value('required', astnode.is_required)

            if astnode.cardinality is not None:
                cmd.set_attribute_value('cardinality', astnode.cardinality)

            # FIXME: this is an approximate solution
            targets = qlast.get_targets(astnode.target)

            if len(targets) > 1:
                new_targets = [
                    utils.ast_to_typeref(
                        t, modaliases=context.modaliases,
                        schema=schema)
                    for t in targets
                ]

                target = cls._create_union_target(
                    schema, context, new_targets, module=source_name.module)
            else:
                target_expr = targets[0]
                if isinstance(target_expr, qlast.TypeName):
                    target = utils.ast_to_typeref(
                        target_expr, modaliases=context.modaliases,
                        schema=schema)
                else:
                    # computable
                    target, base = cmd._parse_computable(
                        target_expr, schema, context)

                    if base is not None:
                        cmd.set_attribute_value(
                            'bases', so.ObjectList.create(schema, [base]),
                        )

                        cmd.set_attribute_value(
                            'is_derived', True
                        )

                        if context.declarative:
                            cmd.set_attribute_value(
                                'declared_inherited', True
                            )

                if (isinstance(target, so.ObjectRef) and
                        target.name == source_name):
                    # Special case for loop links.  Since the target
                    # is the same as the source, we know it's a proper
                    # type.
                    pass
                else:
                    target_type = utils.resolve_typeref(target, schema=schema)
                    if not isinstance(target_type, s_objtypes.ObjectType):
                        raise errors.InvalidLinkTargetError(
                            f'invalid link target, expected object type, got '
                            f'{target_type.__class__.__name__}',
                            context=astnode.target.context
                        )

            if isinstance(cmd, sd.CreateObject):
                cmd.set_attribute_value('target', target)

                if cmd.get_attribute_value('cardinality') is None:
                    cmd.set_attribute_value(
                        'cardinality', qltypes.Cardinality.ONE)

                if cmd.get_attribute_value('required') is None:
                    cmd.set_attribute_value(
                        'required', False)
            else:
                slt = SetLinkType(classname=cmd.classname, type=target)
                slt.set_attribute_value('target', target)
                cmd.add(slt)

            cls._parse_default(cmd)

        if parent_ctx is None:
            # this is an abstract link then
            if cmd.get_attribute_value('default') is not None:
                raise errors.SchemaDefinitionError(
                    f"'default' is not a valid field for an abstact link",
                    context=astnode.context)

    def _apply_refs_fields_ast(self, schema, context, node, refdict):
        if issubclass(refdict.ref_cls, pointers.Pointer):
            for op in self.get_subcommands(metaclass=refdict.ref_cls):
                pname = sn.shortname_from_fullname(op.classname)
                if pname.name not in {'source', 'target'}:
                    self._append_subcmd_ast(schema, node, op, context)
        else:
            super()._apply_refs_fields_ast(schema, context, node, refdict)


class CreateLink(LinkCommand, referencing.CreateReferencedInheritingObject):
    astnode = [qlast.CreateConcreteLink, qlast.CreateLink]
    referenced_astnode = qlast.CreateConcreteLink

    @classmethod
    def _cmd_tree_from_ast(cls, schema, astnode, context):
        cmd = super()._cmd_tree_from_ast(schema, astnode, context)
        cls._process_link_create_or_alter(schema, astnode, context, cmd)
        return cmd

    def _apply_field_ast(self, schema, context, node, op):
        objtype = context.get(LinkSourceCommandContext)

        if op.property == 'required':
            if isinstance(node, qlast.CreateConcreteLink):
                node.is_required = op.new_value
            else:
                node.commands.append(
                    qlast.SetSpecialField(
                        name=qlast.ObjectRef(name='required'),
                        value=op.new_value,
                    )
                )
        elif op.property == 'cardinality':
            node.cardinality = op.new_value
        elif op.property == 'target' and objtype:
            if isinstance(node, qlast.CreateConcreteLink):
                if not node.target:
                    t = op.new_value
                    node.target = utils.typeref_to_ast(schema, t)
            else:
                node.commands.append(
                    qlast.SetLinkType(
                        type=utils.typeref_to_ast(schema, op.new_value)
                    )
                )
        else:
            super()._apply_field_ast(schema, context, node, op)

    def inherit_classref_dict(self, schema, context, refdict):
        if refdict.attr != 'pointers':
            return super().inherit_classref_dict(schema, context, refdict)

        parent_ctx = context.get(LinkSourceCommandContext)
        if parent_ctx is None:
            return super().inherit_classref_dict(schema, context, refdict)

        source_name = parent_ctx.op.classname

        base_prop_name = sn.Name('std::source')
        s_name = sn.get_specialized_name(
            sn.Name('__::source'), self.classname)
        src_prop_name = sn.Name(name=s_name,
                                module=self.classname.module)

        src_prop = lproperties.CreateProperty(
            classname=src_prop_name,
            metaclass=lproperties.Property
        )
        src_prop.update((
            sd.AlterObjectProperty(
                property='name',
                new_value=src_prop_name
            ),
            sd.AlterObjectProperty(
                property='bases',
                new_value=so.ObjectList.create(
                    schema,
                    [so.ObjectRef(name=base_prop_name)],
                ),
            ),
            sd.AlterObjectProperty(
                property='source',
                new_value=so.ObjectRef(
                    name=self.classname
                )
            ),
            sd.AlterObjectProperty(
                property='target',
                new_value=so.ObjectRef(
                    name=source_name
                )
            ),
            sd.AlterObjectProperty(
                property='required',
                new_value=True
            ),
            sd.AlterObjectProperty(
                property='readonly',
                new_value=True
            ),
            sd.AlterObjectProperty(
                property='is_final',
                new_value=True
            ),
            sd.AlterObjectProperty(
                property='is_local',
                new_value=True
            ),
            sd.AlterObjectProperty(
                property='cardinality',
                new_value=qltypes.Cardinality.ONE,
            ),
        ))

        self.add(src_prop)
        schema, _ = src_prop.apply(schema, context)

        base_prop_name = sn.Name('std::target')
        s_name = sn.get_specialized_name(
            sn.Name('__::target'), self.classname)
        tgt_prop_name = sn.Name(name=s_name,
                                module=self.classname.module)

        tgt_prop = lproperties.CreateProperty(
            classname=tgt_prop_name,
            metaclass=lproperties.Property
        )
        tgt_prop.update((
            sd.AlterObjectProperty(
                property='name',
                new_value=tgt_prop_name
            ),
            sd.AlterObjectProperty(
                property='bases',
                new_value=so.ObjectList.create(
                    schema,
                    [so.ObjectRef(name=base_prop_name)],
                ),
            ),
            sd.AlterObjectProperty(
                property='source',
                new_value=so.ObjectRef(
                    name=self.classname
                )
            ),
            sd.AlterObjectProperty(
                property='target',
                new_value=self.get_attribute_value('target'),
            ),
            sd.AlterObjectProperty(
                property='required',
                new_value=False
            ),
            sd.AlterObjectProperty(
                property='readonly',
                new_value=True
            ),
            sd.AlterObjectProperty(
                property='is_final',
                new_value=True
            ),
            sd.AlterObjectProperty(
                property='is_local',
                new_value=True
            ),
            sd.AlterObjectProperty(
                property='cardinality',
                new_value=qltypes.Cardinality.ONE,
            ),
        ))

        self.add(tgt_prop)
        schema, _ = tgt_prop.apply(schema, context)

        return super().inherit_classref_dict(schema, context, refdict)


class RenameLink(LinkCommand, sd.RenameObject):
    pass


class RebaseLink(LinkCommand, inheriting.RebaseInheritingObject):
    pass


class SetLinkType(pointers.SetPointerType,
                  schema_metaclass=Link,
                  referrer_context_class=LinkSourceCommandContext):

    astnode = qlast.SetLinkType


class SetTargetDeletePolicy(sd.Command):
    astnode = qlast.OnTargetDelete

    @classmethod
    def _cmd_from_ast(cls, schema, astnode, context):
        return sd.AlterObjectProperty(property='on_target_delete')

    @classmethod
    def _cmd_tree_from_ast(cls, schema, astnode, context):
        cmd = super()._cmd_tree_from_ast(schema, astnode, context)
        cmd.new_value = astnode.cascade
        return cmd


class AlterLink(LinkCommand, referencing.AlterReferencedInheritingObject):
    astnode = [qlast.AlterLink, qlast.AlterConcreteLink]
    referenced_astnode = qlast.AlterConcreteLink

    @classmethod
    def _cmd_tree_from_ast(cls, schema, astnode, context):
        cmd = super()._cmd_tree_from_ast(schema, astnode, context)
        cls._process_link_create_or_alter(schema, astnode, context, cmd)

        return cmd


class DeleteLink(LinkCommand, inheriting.DeleteInheritingObject):
    astnode = [qlast.DropLink, qlast.DropConcreteLink]
    referenced_astnode = qlast.DropConcreteLink

    def _canonicalize(self, schema, context, scls):
        super()._canonicalize(schema, context, scls)

        target = scls.get_target(schema)

        # A link may only target a view only inside another view,
        # which means that the target view must be dropped along
        # with this link.
        if (target is not None
                and target.is_view(schema)
                and target.get_view_is_persistent(schema)):

            Cmd = sd.ObjectCommandMeta.get_command_class_or_die(
                sd.DeleteObject, type(target))

            del_cmd = Cmd(classname=target.get_name(schema))
            del_cmd._canonicalize(schema, context, target)
            self.add(del_cmd)
