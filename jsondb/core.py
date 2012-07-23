# -*- coding: utf-8 -*-

"""
    jsondb
    ~~~~~~

    Turn the json data into a database.
    Then query the data using JSONPath.
"""

import os
import tempfile
import re
import weakref

import logging
logger = logging.getLogger(__file__)

try:
    import simplejson as json
except:
    import json

from datatypes import *
import backends
import jsonquery

from error import *

class Nothing:
    pass


def get_type_class(datatype):
    if datatype == DICT:
        cls = DictQueryable
    elif datatype == LIST:
        cls = ListQueryable
    elif datatype in (STR, UNICODE):
        cls = StringQueryable
    else:
        cls = PlainQueryable

    return cls


class QueryResult(object):
    def __init__(self, seq, queryset):
        self.seq = seq
        self.queryset = weakref.proxy(queryset)

    def getone(self):
        try:
            return self.queryset._make(next(self.seq))
        except StopIteration:
            return None

    def itervalues(self):
        for row in self.seq:
            yield self.queryset._make(row).data()

    def values(self):
        return list(self.itervalues())

    def __iter__(self):
        for row in self.seq:
            yield self.queryset._make(row)


class Queryable(object):
    def __init__(self,  backend, link_key=None, root=-1, datatype=None, data=Nothing()):
        self.backend = weakref.proxy(backend) if isinstance(backend, weakref.ProxyTypes) else backend
        self.link_key = link_key or '@__link__'
        self.query_path_cache = {}
        self.root = root
        self._data = data
        self.datatype = datatype

    def __hash__(self):
        return self.root

    def __len__(self):
        raise NotImplemented

    def _make(self, node):
        root_id = node.id
        _type = node.type

        cls = get_type_class(_type)

        if _type in (LIST, DICT):
            row = self.backend.get_row(node.id)
            data = row['value']
        else:
            data = Nothing()
        result = cls(backend=self.backend, link_key=self.backend.get_link_key(), root=root_id, datatype=_type, data=data)
        return result

    def __getitem__(self, key):
        """
        Same with query, but mainly for direct access. 
        1. Does not require the $ prefix
        2. Only query for direct children
        3. Only retrive one child
        """
        if isinstance(key, bool):
            raise UnsupportedOperation
        elif isinstance(key, basestring):
            if not key.startswith('$'):
                key = '$.%s' % key
        elif isinstance(key, (int, long)):
            key = '$.[%s]' % key
        else:
            raise UnsupportedOperation

        node = self.query(key, one=True).getone()
        return node

    def store(self, data, parent=None):
        # TODO: store raw json data into a node.
        raise NotImplemented
 
    def feed(self, data, parent=None):
        """Append data to the specified parent.

        Parent may be a dict or list.

        Returns list of ids:
        * value.id when appending {key : value} to a dict
        * each_child.id when appending a list to a list
        """
        #logger.debug('feed start')
        if parent is None:
            parent = self.root
        # TODO: should be in a transaction
        id_list, pending_list = self._feed(data, parent)
        self.backend.batch_insert(pending_list)
        #logger.debug('feed end')
        return id_list

    def _feed(self, data, parent_id, real_parent_id=None):
        parent = self.backend.get_row(parent_id)
        parent_type = parent['type']

        if real_parent_id is None:
            real_parent_id = parent_id

        id_list = []
        pending_list = []
 
        _type = TYPE_MAP.get(type(data))
        #logger.debug('feeding %s(%s) into %s(%s)' % (repr(data), DATA_TYPE_NAME[_type], parent_id, DATA_TYPE_NAME[parent_type]))

        if _type == DICT:
            if parent_type == DICT:
                hash_id = parent_id
            elif parent_type in (LIST, KEY):
                hash_id = self.backend.insert((parent_id, _type, 0,))
            elif parent_id != self.root:
                print parent_id, parent_type
                raise IllegalTypeError, 'Parent node should be either DICT or LIST.'

            for key, value in data.iteritems():
                if key == self.link_key:
                    self.backend.update_link(hash_id, value)
                    continue
                key_id, value_id = self.backend.find_key(key, hash_id) if parent_type == DICT else (None, None)
                if key_id is not None:
                    self.backend.remove(key_id)
                else:
                    key_id = self.backend.insert((hash_id, KEY, key,))
                _ids, _pendings = self._feed(value, key_id, real_parent_id=hash_id)
                id_list += _ids
                pending_list += _pendings

        elif _type == LIST:
            # TODO: need to distinguish *appending* from *merging* 
            #       now we always assume it's appending
            hash_id = self.backend.insert((parent_id, _type, 0,))
            id_list.append(hash_id)
            for x in data:
                _ids, _pendings = self._feed(x, hash_id)
                id_list += _ids
                pending_list += _pendings
        else:
            pending_list.append((parent_id, _type, data,))

        if parent_type in (DICT, LIST, KEY):
            #  Use the value field to store the length of LIST / DICT.
            self.backend.increase_value(real_parent_id, 1)

        return id_list, pending_list

    def query(self, path, parent=None, one=False):
        """
        Query the data.
        """
        if parent is None:
            parent = self.root

        cache = self.query_path_cache.get(path)
        if cache:
            ast = json.loads(cache)
        else:
            ast = jsonquery.parse(path)
            self.query_path_cache[path] = json.dumps(ast)
        rslt = self.backend.jsonpath(ast=ast, parent=parent, one=one)
        return QueryResult(rslt, self)

    xpath = query

    def build_node(self, row):
        node = get_initial_data(row['type'])
        _type = row['type']
        _value = row['value']

        if _type == KEY:
            for child in self.backend.iter_children(row['id']):
                node = {_value : self.build_node(child)}
                break

        elif _type in (LIST, DICT):
            func = node.update if _type == DICT else node.append
            for child in self.backend.iter_children(row['id']):
                func(self.build_node(child))

        elif _type == STR:
            node = _value

        elif _type == UNICODE:
            node = _value

        elif _type == BOOL:
            node = bool(_value)

        elif _type == INT:
            node = int(_value)

        elif _type == FLOAT:
            node = float(_value)

        elif _type == NIL:
            node = None

        return node

    def id(self):
        return self.root

    def data(self, update=False):
        if not self.datatype in (LIST, DICT):
            if not update and not isinstance(self._data, Nothing):
                return self._data
        root = self.backend.get_row(self.root)
        _data = self.build_node(root) if root else DATA_INITIAL[self.datatype]
        if not self.datatype in (LIST, DICT):
            self._data = _data
        return _data

    def link(self):
        root = self.backend.get_row(self.root)
        return root['link']

    def check_type(self, data):
        new_type = TYPE_MAP.get(type(data))
        if self.datatype in (LIST, DICT):
            return TYPE_MAP.get(type(data)) == self.datatype
        return True

    def update(self, data):
        if not self.check_type(data):
            # TODO: different type
            raise Error
        self.backend.set_value(self.root, data)

    def dumps(self):
        """Dump the json data"""
        return json.dumps(self.data())

    def dump(self, filepath):
        """Dump the json data to a file"""
        with open(filepath, 'wb') as f:
            root = self.backend.get_row(self.root)
            rslt = self.build_node(root) if root else ''
            f.write(repr(rslt))

    def commit(self):
        self.backend.commit()

    def close(self):
        self.backend.close()

    def set_value(self, id, value):
        self.backend.set_value(id, value)

    def _get_value(self):
        row = self.backend.get_row(self.root)
        data = row['value']
        return data

    def update_link(self, rowid, link=None):
        self.backend.update_link(rowid, link)

    def get_row(self, rowid):
        row = self.backend.get_row(rowid)
        if not row:
            return None
        return Result.from_row(row)

    def get_path(self):
        return self.backend.get_path()


class SequenceQueryable(Queryable):
    def __len__(self):
        return self._get_value()

    def __getitem__(self, key):
        if isinstance(key, slice):
            rslt = self.backend.iter_slice(self.root, key.start, key.stop, key.step)
            return QueryResult(rslt, self)
        return super(SequenceQueryable, self).__getitem__(key)

    def __setitem__(self, key, value):
        """
        If the key is not found, it will be created from the value.
        when self is a dict:
            update or create a child entry.
            dict[key] = value
        when self is a list:
            li[index] = value
        when self is a simple type:
            path.value = value

        :param key: key to set data

        :param value: value to set to key
        """
        node = self[key]
        if not node:
            if self.datatype == DICT:
                self.update({key: value})
            elif self.datatype == LIST and isinstance(key, (int, long)):
                if abs(key) >= len(self):
                    raise IndexError
                # TODO: feed list
            return

        node.update(value)

    def __delitem__(self, key):
        # TODO: 
        pass

    def __iter__(self):
        # TODO: 
        pass

    def __reversed__(self):
        # TODO: 
        pass

    def __contains__(self, item):
        # TODO: hash
        return False

    def __concat__(self, other):
        # TODO: 
        pass

    def __add__(self, other):
        # TODO: 
        pass

    def __iadd__(self, other):
        # TODO: 
        pass

    def __isub__(self, other):
        # TODO: 
        pass

    def __imul__(self, other):
        # TODO: 
        pass

    def __imul__(self, other):
        # TODO: 
        pass


class ListQueryable(SequenceQueryable):
    def append(self, data):
        self.feed(data)

    def __getitem__(self, key):
        if isinstance(key, (int, long)):
            if abs(key) >= len(self):
                raise IndexError
            rslt = self.backend.get_nth_child(self.root, key)
            return self._make(rslt)
        return super(ListQueryable, self).__getitem__(key)


class DictQueryable(SequenceQueryable):
    def update(self, data):
        self.feed(data)

class StringQueryable(SequenceQueryable):
    def __len__(self):
        return len(self.data())

class PlainQueryable(Queryable):
    def __cmp__(self, other):
        pass

    def __eq__(self, other):
        pass

    def __ne__(self, other):
        pass

    def __lt__(self, other):
        pass

    def __gt__(self, other):
        pass

    def __le__(self, other):
        pass

    def __ge__(self, other):
        pass

    def __pos__(self, other):
        pass

    def __neg__(self, other):
        pass

    def __abs__(self, other):
        pass
 
    def __invert__(self, other):
        pass
 
    def __add__(self, other):
        pass

    def __sub__(self, other):
        pass

    def __mul__(self, other):
        pass

    def __floordiv__(self, other):
        pass

    def __div__(self, other):
        pass

    def __truediv__(self, other):
        pass

    def __mod_(self, other):
        pass

    def __pow__(self, other):
        pass

    def __lshift__(self, other):
        pass

    def __rshift__(self, other):
        pass

    def __and__(self, other):
        pass

    def __or__(self, other):
        pass

    def __xor__(self, other):
        pass

    def __radd__(self, other):
        pass

    def __rsub__(self, other):
        pass

    def __rmul__(self, other):
        pass

    def __rfloordiv__(self, other):
        pass

    def __rdiv__(self, other):
        pass

    def __rtruediv__(self, other):
        pass

    def __rmod_(self, other):
        pass

    def __rpow__(self, other):
        pass

    def __rlshift__(self, other):
        pass

    def __rrshift__(self, other):
        pass

    def __rand__(self, other):
        pass

    def __ror__(self, other):
        pass

    def __rxor__(self, other):
        pass

    def __iadd__(self, other):
        pass

    def __isub__(self, other):
        pass

    def __imul__(self, other):
        pass

    def __ifloordiv__(self, other):
        pass

    def __idiv__(self, other):
        pass

    def __itruediv__(self, other):
        pass

    def __imod_(self, other):
        pass

    def __ipow__(self, other):
        pass

    def __ilshift__(self, other):
        pass

    def __irshift__(self, other):
        pass

    def __iand__(self, other):
        pass

    def __ior__(self, other):
        pass

    def __ixor__(self, other):
        pass


class EmptyNode(Queryable):
    pass

