import ldap
from plumber import (
    plumber,
    plumb,
    default,
    extend,
    finalize,
    Part,
)
from zope.interface import implements
from node.base import AttributedNode
from node.locking import locktree
from node.aliasing import (
    AliasedNodespace,
    DictAliaser,
)
from node.parts import (
    NodeChildValidate,
    Nodespaces,
    Adopt,
    Attributes,
    DefaultInit,
    Nodify,
    Storage,
    OdictStorage,
)
from node.ext.ugm import (
    User as BaseUserPart,
    Group as BaseGroupPart,
    Users as UsersPart,
    Groups as BaseGroupsPart,
    Ugm as UgmPart,
)
from node.ext.ldap.interfaces import (
    ILDAPGroupsConfig as IGroupsConfig,
    ILDAPUsersConfig as IUsersConfig,
)
from node.ext.ldap.scope import ONELEVEL
from node.ext.ldap.debug import debug
from node.ext.ldap.bbb import LDAPNode


class PrincipalsConfig(object):
    """Superclass for UsersConfig, GroupsConfig and RolesConfig (later)
    """
    
    def __init__(self,
            baseDN='',
            newDN='',
            attrmap={},
            scope=ONELEVEL,
            queryFilter='',
            objectClasses=[],
            member_relation=''):
        self.baseDN = baseDN
        self.newDN = newDN or baseDN
        self.attrmap = attrmap
        self.scope = scope
        self.queryFilter = queryFilter
        self.objectClasses = objectClasses
        # XXX: never used. what was this supposed for?
        self.member_relation = member_relation


class UsersConfig(PrincipalsConfig):
    """Define how users look and where they are.
    """
    implements(IUsersConfig)


class GroupsConfig(PrincipalsConfig):
    """Define how groups look and where they are.
    """
    implements(IGroupsConfig)


class PrincipalPart(Part):
    
    @extend
    def __init__(self, context, attraliaser):
        self.context = context
        self.attraliaser = attraliaser
    
    @default
    def ldap_attributes_factory(self, name=None, parent=None):
        if self.attraliaser is None:
            return self.context.attrs
        aliased_attrs = AliasedNodespace(self.context.attrs, self.attraliaser)
        aliased_attrs.allow_non_node_childs = True
        return aliased_attrs
    
    attributes_factory = finalize(ldap_attributes_factory)
    
    @default
    def add_role(self, role):
        self.parent.parent.add_role(role, self)
    
    @default
    def remove_role(self, role):
        self.parent.parent.remove_role(role, self)
    
    @default
    @property
    def roles(self):
        return self.parent.parent.roles(self)
    
    @default
    def __call__(self):
        self.context()


class UserPart(PrincipalPart, BaseUserPart):
    
    @default
    @property
    def groups(self):
        groups = self.parent.parent.groups
        criteria = {
            groups._member_attribute: self.context.DN,
        }
        res = groups.context.search(criteria=criteria)
        ret = list()
        for id in res:
            ret.append(groups[id])
        return ret

    
class GroupPart(PrincipalPart, BaseGroupPart):
    
    @default
    @property
    def _member_format(self):
        return self.parent._member_format
    
    @default
    @property
    def _member_attribute(self):
        return self.parent._member_attribute
    
    @default
    @property
    def users(self):
        return [self.parent.parent.users[id] for id in self.member_ids]
    
    @default
    @property
    def member_ids(self):
        members = list()
        for member in self.context.attrs[self._member_attribute]:
            if member in ['nobody', 'cn=nobody']:
                continue
            members.append(member)
        if self._member_format == FORMAT_DN:
            return [self.parent.parent.users.idbydn(dn) for dn in members]
        if self._member_format == FORMAT_UID:
            return members
        raise Exception(u"Unknown member value format.")


class User(object):
    __metaclass__ = plumber
    __plumbing__ = (
        Nodespaces,
        Attributes,
        Nodify,
        UserPart,
    )
    
    def __getitem__(self, key):
        raise NotImplementedError(u"User object is a leaf.")
    
    def __setitem__(self, key, value):
        raise NotImplementedError(u"User object is a leaf.")
    
    def __delitem__(self, key):
        raise NotImplementedError(u"User object is a leaf.")
    
    def __iter__(self):
        return iter([])


class Group(object):
    __metaclass__ = plumber
    __plumbing__ = (
        NodeChildValidate,
        Nodespaces,
        Attributes,
        Nodify,
        GroupPart,
    )
    
    def __contains__(self, key):
        for id in self:
            if id == key:
                return True
        return False
    
    @locktree
    def __setitem__(self, key, value):
        if key != value.name:
            raise RuntimeError(u"Id mismatch at attempt to add group member.")
        if not key in self.member_ids:
            if self._member_format == FORMAT_DN:
                val = self.parent.parent.users.context.child_dn(key)
            elif self._member_format == FORMAT_UID:
                val = key
            # self.context.attrs[self._member_attribute].append won't work here
            # issue in LDAPNodeAttributes, does not recognize changed this way.
            old = self.context.attrs[self._member_attribute]
            self.context.attrs[self._member_attribute] = old + [val]
            # XXX: call here immediately?
            self.context()
    
    def __getitem__(self, key):
        if key not in self:
            raise KeyError(key)
        return self.parent.parent.users[key]
    
    @locktree
    def __delitem__(self, key):
        if key not in self:
            raise KeyError(key)
        if self._member_format == FORMAT_DN:
            val = self.parent.parent.users.context.child_dn(key)
        elif self._member_format == FORMAT_UID:
            val = key
        # self.context.attrs[self._member_attribute].remove won't work here
        # issue in LDAPNodeAttributes, does not recognize changed this way.
        members = self.context.attrs[self._member_attribute]
        members.remove(val)
        self.context.attrs[self._member_attribute] = members
        # XXX: call here immediately?
        self.context()
    
    def __iter__(self):
        for id in self.member_ids:
            yield id


class PrincipalsPart(Part):
    principals_factory = default(None)
    principal_attrmap = default(None)
    principal_attraliaser = default(None)
    
    @extend
    def __init__(self, props, cfg):
        self.context = LDAPNode(name=cfg.baseDN, props=props)
        self.context._child_filter = cfg.queryFilter
        self.context._child_scope = int(cfg.scope)
        self.context._child_objectClasses = cfg.objectClasses
        self.context._key_attr = cfg.attrmap['id']
        self.context._rdn_attr = cfg.attrmap['rdn']
        
        # what's a member_relation?
        if cfg.member_relation:
            self.context._child_relation = cfg.member_relation
        
        self.context._seckey_attrs = ('dn',)
        if cfg.attrmap.get('login') \
          and cfg.attrmap['login'] != cfg.attrmap['id']:
            self.context._seckey_attrs += (cfg.attrmap['login'],)
        
        self.principal_attrmap = cfg.attrmap
        self.principal_attraliaser = DictAliaser(cfg.attrmap)
    
    @default
    def idbydn(self, dn):
        """Return a principal's id for a given dn.

        Raise KeyError if not enlisted.
        """
        self.context.keys()
        idsbydn = self.context._seckeys['dn']
        try:
            return idsbydn[dn]
        except KeyError:
            # It's possible that we got a different string resulting
            # in the same DN, as every components can have individual
            # comparison rules (see also node.ext.ldap.bbb.LDAPNode.DN).
            # We leave the job to LDAP and try again with the resulting DN.
            #
            # This was introduced because a customer has group
            # member attributes where the DN string differs.
            #
            # This would not be necessary, if an LDAP directory
            # is consistent, i.e does not use different strings to
            # talk about the same.
            #
            # Normalization of DN in python would also be a
            # solution, but requires implementation of all comparison
            # rules defined in schemas. Maybe we can retrieve them
            # from LDAP.
            search = self.context.ldap_session.search
            try:
                dn = search(baseDN=dn)[0][0]
            except ldap.NO_SUCH_OBJECT:
                raise KeyError(dn)
            return idsbydn[dn]

    @extend
    @property
    def ids(self):
        # XXX: do we really need this?
        return self.context.keys()

    @default
    def __delitem__(self, key):
        del self.context[key]

    @default
    def __getitem__(self, key):
        # XXX: should use lazynodes caching, for now:
        # users['foo'] is not users['foo']
        principal = self.principal_factory(
            self.context[key],
            attraliaser=self.principal_attraliaser)
        principal.__name__ = self.context[key].__name__
        principal.__parent__ = self
        return principal

    @default
    def __iter__(self):
        return self.context.__iter__()

    @default
    def __setitem__(self, name, vessel):
        try:
            attrs = vessel.attrs
        except AttributeError:
            raise ValueError(u"no attributes found, cannot convert.")
        if name in self:
            raise KeyError(u"Key already exists: '%s'." % (name,))
        nextvessel = AttributedNode()
        nextvessel.__name__ = name
        nextvessel.attribute_access_for_attrs = False
        principal = self.principal_factory(
            nextvessel,
            attraliaser=self.principal_attraliaser)
        principal.__name__ = name
        principal.__parent__ = self
        # XXX: cache
        for key, val in attrs.iteritems():
            principal.attrs[key] = val
        self.context[name] = nextvessel
    
    @default
    def __call__(self):
        self.context()

    @default
    def _alias_dict(self, dct):
        if dct is None:
            return None
        
        # this code does not work if multiple keys map to same value
        #alias = self.principal_attraliaser.alias
        #aliased_dct = dict(
        #    [(alias(key), val) for key, val in dct.iteritems()])
        #return aliased_dct
        
        # XXX: maybe some generalization in aliaser needed
        ret = dict()
        for key, val in self.principal_attraliaser.iteritems():
            for k, v in dct.iteritems():
                if val == k:
                    ret[key] = v
        return ret

    @default
    def _unalias_list(self, lst):
        if lst is None:
            return None
        unalias = self.principal_attraliaser.unalias
        return [unalias(x) for x in lst]

    @default
    def _unalias_dict(self, dct):
        if dct is None:
            return None
        unalias = self.principal_attraliaser.unalias
        unaliased_dct = dict(
                [(unalias(key), val) for key, val in dct.iteritems()]
                )
        return unaliased_dct

    @default
    def search(self, criteria=None, attrlist=None, exact_match=False,
               or_search=False):
        # XXX: stripped down for now, compare to LDAPNode.search
        # XXX: are single values always lists in results?
        #      is this what we want -> yes!
        results = self.context.search(
            criteria=self._unalias_dict(criteria),
            attrlist=self._unalias_list(attrlist),
            exact_match=exact_match,
            or_search=or_search)
        if attrlist is None:
            return results
        aliased_results = \
            [(id, self._alias_dict(attrs)) for id, attrs in results]
        return aliased_results
    
    @default
    def create(self, _id, **kw):
        vessel = AttributedNode()
        for k, v in kw.items():
            vessel.attrs[k] = v
        self[_id] = vessel
        return self[_id]


class Users(object):
    __metaclass__ = plumber
    __plumbing__ = (
        NodeChildValidate,
        Nodespaces,
        Adopt,
        PrincipalsPart,
        Attributes,
        Nodify,
        UsersPart,
    )
    
    principal_factory = User

    def __delitem__(self, id):
        user = self[id]
        for group in user.groups:
            del group[user.name]
        del self.context[id]
    
    @debug(['authentication'])
    def authenticate(self, id=None, pw=None):
        id = self.context._seckeys.get(
            self.principal_attrmap.get('login'), {}).get(id, id)
        try:
            userdn = self.context.child_dn(id)
        except KeyError:
            return False
        return self.context._session.authenticate(userdn, pw) and id or False

    @debug(['authentication'])
    def passwd(self, id, oldpw, newpw):
        self.context._session.passwd(self.context.child_dn(id), oldpw, newpw)


class GroupsPart(BaseGroupsPart):
    
    @plumb
    def __setitem__(_next, self, key, vessel):
        vessel.attrs.setdefault(
            self._member_attribute, []).insert(0, 'cn=nobody')
        _next(self, key, vessel)


FORMAT_DN = 0
FORMAT_UID = 1

class Groups(object):
    __metaclass__ = plumber
    __plumbing__ = (
        NodeChildValidate,
        Nodespaces,
        Adopt,
        PrincipalsPart,
        Attributes,
        Nodify,
        GroupsPart,
    )
    
    principal_factory = Group
    
    @property
    def _member_format(self):
        obj_cl = self.context._child_objectClasses
        if 'groupOfNames' in obj_cl:
            return FORMAT_DN
        if 'groupOfUniqueNames' in obj_cl:
            return FORMAT_DN
        if 'posixGroup' in obj_cl:
            return FORMAT_UID
        raise Exception(u"Unknown format")
    
    @property
    def _member_attribute(self):
        obj_cl = self.context._child_objectClasses
        if 'groupOfNames' in obj_cl:
            return 'member'
        if 'groupOfUniqueNames' in obj_cl:
            return 'uniqueMember'
        if 'posixGroup' in obj_cl:
            return 'memberUid'
        raise Exception(u"Unknown member attribute")


class Ugm(object):
    __metaclass__ = plumber
    __plumbing__ = (
        NodeChildValidate,
        Nodespaces,
        Adopt,
        Attributes,
        DefaultInit,
        Nodify,
        UgmPart,
        OdictStorage,
    )
    
    def __init__(self, name=None, parent=None, props=None,
                 ucfg=None, gcfg=None, rcfg=None):
        """
        ``name``
            node name
            
        ``parent``
            node parent
            
        ``props``
            LDAPProps
        
        ``ucfg``
            UsersConfig
        
        ``gcfg``
            GroupsConfig
        
        ``rcfg``
            RolesConfig XXX: not yet
        """
        self.__name__ = name
        self.__parent__ = parent
        self.props = props
        self.ucfg = ucfg
        self.gcfg = gcfg
        self.rcfg = rcfg
    
    def __getitem__(self, key):
        if not key in self.storage:
            if key == 'users':
                self['users'] = Users(self.props, self.ucfg)
            else:
                self['groups'] = Groups(self.props, self.gcfg)
        return self.storage[key]
    
    @locktree
    def __setitem__(self, key, value):
        self._chk_key(key)
        self.storage[key] = value
    
    def __delitem__(self, key):
        raise NotImplementedError(u"Operation forbidden on this node.")
    
    def __iter__(self):
        for key in ['users', 'groups']:
            yield key
    
    def __call__(self):
        self.users()
        self.groups()
    
    @property
    def users(self):
        return self['users']
    
    @property
    def groups(self):
        return self['groups']
    
    def add_role(self, role, principal):
        # XXX
        raise NotImplementedError(u"not yet")
    
    def remove_role(self, role, principal):
        # XXX
        raise NotImplementedError(u"not yet")
        
    def roles(self, principal):
        # XXX
        raise NotImplementedError(u"not yet")
    
    def _chk_key(self, key):
        if not key in ['users', 'groups']:
            raise KeyError(key)