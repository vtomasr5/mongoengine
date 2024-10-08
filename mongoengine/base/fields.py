import operator
import warnings
import weakref

from bson import DBRef, ObjectId, SON
import pymongo

from mongoengine.common import _import_class
from mongoengine.errors import ValidationError

from mongoengine.base.common import ALLOW_INHERITANCE
from mongoengine.base.datastructures import BaseDict, BaseList

__all__ = ("BaseField", "ComplexBaseField", "ObjectIdField", "GeoJsonBaseField")


class BaseField(object):
    """A base class for fields in a MongoDB document. Instances of this class
    may be added to subclasses of `Document` to define a document's schema.

    .. versionchanged:: 0.5 - added verbose and help text
    """

    name = None
    _geo_index = False
    _auto_gen = False  # Call `generate` to generate a value
    _auto_dereference = True

    # These track each time a Field instance is created. Used to retain order.
    # The auto_creation_counter is used for fields that MongoEngine implicitly
    # creates, creation_counter is used for all user-specified fields.
    creation_counter = 0
    auto_creation_counter = -1

    def __init__(self, db_field=None, name=None, required=False, default=None,
                 unique=False, unique_with=None, primary_key=False,
                 validation=None, choices=None, verbose_name=None,
                 help_text=None):
        """
        :param db_field: The database field to store this field in
            (defaults to the name of the field)
        :param name: Depreciated - use db_field
        :param required: If the field is required. Whether it has to have a
            value or not. Defaults to False.
        :param default: (optional) The default value for this field if no value
            has been set (or if the value has been unset).  It Can be a
            callable.
        :param unique: Is the field value unique or not.  Defaults to False.
        :param unique_with: (optional) The other field this field should be
            unique with.
        :param primary_key: Mark this field as the primary key. Defaults to False.
        :param validation: (optional) A callable to validate the value of the
            field.  Generally this is deprecated in favour of the
            `FIELD.validate` method
        :param choices: (optional) The valid choices
        :param verbose_name: (optional)  The verbose name for the field.
            Designed to be human readable and is often used when generating
            model forms from the document model.
        :param help_text: (optional) The help text for this field and is often
            used when generating model forms from the document model.
        """
        self.name = None # filled in by document
        self.db_field = db_field
        self.required = required or primary_key
        self.default = default
        self.unique = bool(unique or unique_with)
        self.unique_with = unique_with
        self.primary_key = primary_key
        if self.primary_key:
            if self.db_field:
                raise ValueError("Can't use primary_key in conjunction with db_field.")
            self.db_field = '_id'
        self.validation = validation
        self.choices = choices
        self.verbose_name = verbose_name
        self.help_text = help_text

        # Adjust the appropriate creation counter, and save our local copy.
        if self.db_field == '_id':
            self.creation_counter = BaseField.auto_creation_counter
            BaseField.auto_creation_counter -= 1
        else:
            self.creation_counter = BaseField.creation_counter
            BaseField.creation_counter += 1

    def __get__(self, instance, owner):
        if instance is None:
            # Document class being used rather than a document object
            return self
        else:
            name = self.name
            data = instance._internal_data
            if not name in data:
                if instance._lazy and name != instance._meta['id_field']:
                    # We need to fetch the doc from the database.
                    instance.reload()
                    # Reloading changes our internal data pointer.
                    data = instance._internal_data
                db_field = instance._db_field_map.get(name, name)
                try:
                    db_value = instance._db_data[db_field]
                except (TypeError, KeyError):
                    value = self.default() if callable(self.default) else self.default
                else:
                    value = self.to_python(db_value)

                if hasattr(self, 'value_for_instance'):
                    value = self.value_for_instance(value, instance)
                data[name] = value

            return data[name]

    def __set__(self, instance, value):
        """Descriptor for assigning a value to a field in a document.
        """

        if instance._lazy:
            # Fetch the from the database before we assign to a lazy object.
            instance.reload()

        name = self.name

        value = self.from_python(value)
        if hasattr(self, 'value_for_instance'):
            value = self.value_for_instance(value, instance)
        try:
            has_changed = name not in instance._internal_data or instance._internal_data[name] != value
        except: # Values can't be compared eg: naive and tz datetimes
            has_changed = True

        if has_changed:
            instance._mark_as_changed(name)

        instance._internal_data[name] = value

    def error(self, message="", errors=None, field_name=None):
        """Raises a ValidationError.
        """
        field_name = field_name if field_name else self.name
        raise ValidationError(message, errors=errors, field_name=field_name)

    def to_python(self, value):
        """Convert a MongoDB-compatible type to a Python type.
        """
        return value

    def to_mongo(self, value):
        """Convert a Python type to a MongoDB-compatible type.
        """
        return value

    def from_python(self, value):
        """Convert a raw Python value (in an assignment) to the internal
        Python representation.
        """
        if value == None:
            return self.default() if callable(self.default) else self.default
        return value

    def prepare_query_value(self, op, value):
        """Prepare a value that is being used in a query for PyMongo.
        """
        return value

    def validate(self, value, clean=True):
        """Perform validation on a value.
        """
        pass

    def _validate(self, value, **kwargs):
        Document = _import_class('Document')
        EmbeddedDocument = _import_class('EmbeddedDocument')
        # check choices
        if self.choices:
            is_cls = isinstance(value, (Document, EmbeddedDocument))
            value_to_check = value.__class__ if is_cls else value
            err_msg = 'an instance' if is_cls else 'one'
            if isinstance(self.choices[0], (list, tuple)):
                option_keys = [k for k, v in self.choices]
                if value_to_check not in option_keys:
                    msg = ('Value must be %s of %s' %
                           (err_msg, str(option_keys)))
                    self.error(msg)
            elif value_to_check not in self.choices:
                msg = ('Value must be %s of %s' %
                       (err_msg, str(self.choices)))
                self.error(msg)

        # check validation argument
        if self.validation is not None:
            if callable(self.validation):
                if not self.validation(value):
                    self.error('Value does not match custom validation method')
            else:
                raise ValueError('validation argument for "%s" must be a '
                                 'callable.' % self.name)

        self.validate(value, **kwargs)


class ComplexBaseField(BaseField):
    """Handles complex fields, such as lists / dictionaries.

    Allows for nesting of embedded documents inside complex types.
    Handles the lazy dereferencing of a queryset by lazily dereferencing all
    items in a list / dict rather than one at a time.

    .. versionadded:: 0.5
    """

    field = None

    def to_python(self, value):
        """Convert a MongoDB-compatible type to a Python type.
        """
        Document = _import_class('Document')

        if isinstance(value, str):
            return value

        if hasattr(value, 'to_python'):
            return value.to_python()

        is_list = False
        if not hasattr(value, 'items'):
            try:
                is_list = True
                value = dict([(k, v) for k, v in enumerate(value)])
            except TypeError:  # Not iterable return the value
                return value

        if self.field:
            value_dict = dict([(key, self.field.to_python(item))
                               for key, item in list(value.items())])
        else:
            value_dict = {}
            for k, v in list(value.items()):
                if isinstance(v, Document):
                    # We need the id from the saved object to create the DBRef
                    if v.pk is None:
                        self.error('You can only reference documents once they'
                                   ' have been saved to the database')
                    collection = v._get_collection_name()
                    value_dict[k] = DBRef(collection, v.pk)
                elif hasattr(v, 'to_python'):
                    value_dict[k] = v.to_python()
                else:
                    value_dict[k] = self.to_python(v)

        if is_list:  # Convert back to a list
            return [v for k, v in sorted(list(value_dict.items()),
                                         key=operator.itemgetter(0))]
        return value_dict

    def to_mongo(self, value):
        """Convert a Python type to a MongoDB-compatible type.
        """
        Document = _import_class("Document")
        EmbeddedDocument = _import_class("EmbeddedDocument")
        GenericReferenceField = _import_class("GenericReferenceField")

        if isinstance(value, str):
            return value

        if hasattr(value, 'to_mongo'):
            if isinstance(value, Document):
                return GenericReferenceField().to_mongo(value)
            cls = value.__class__
            val = value.to_mongo()
            # If we its a document thats not inherited add _cls
            if (isinstance(value, EmbeddedDocument)):
                val['_cls'] = cls.__name__
            return val

        is_list = False
        if not hasattr(value, 'items'):
            try:
                is_list = True
                value = dict([(k, v) for k, v in enumerate(value)])
            except TypeError:  # Not iterable return the value
                return value

        if self.field:
            value_dict = dict([(key, self.field.to_mongo(item))
                               for key, item in value.items()])
        else:
            value_dict = {}
            for k, v in value.items():
                if isinstance(v, Document):
                    # We need the id from the saved object to create the DBRef
                    if v.pk is None:
                        self.error('You can only reference documents once they'
                                   ' have been saved to the database')

                    # If its a document that is not inheritable it won't have
                    # any _cls data so make it a generic reference allows
                    # us to dereference
                    meta = getattr(v, '_meta', {})
                    allow_inheritance = (
                        meta.get('allow_inheritance', ALLOW_INHERITANCE)
                        is True)
                    if not allow_inheritance and not self.field:
                        value_dict[k] = GenericReferenceField().to_mongo(v)
                    else:
                        collection = v._get_collection_name()
                        value_dict[k] = DBRef(collection, v.pk)
                elif hasattr(v, 'to_mongo'):
                    cls = v.__class__
                    val = v.to_mongo()
                    # If we its a document thats not inherited add _cls
                    if (isinstance(v, (Document, EmbeddedDocument))):
                        val['_cls'] = cls.__name__
                    value_dict[k] = val
                else:
                    value_dict[k] = self.to_mongo(v)

        if is_list:  # Convert back to a list
            return [v for k, v in sorted(list(value_dict.items()),
                                         key=operator.itemgetter(0))]
        return value_dict

    def validate(self, value):
        """If field is provided ensure the value is valid.
        """
        errors = {}
        if self.field:
            if hasattr(value, 'iteritems') or hasattr(value, 'items'):
                sequence = iter(value.items())
            else:
                sequence = enumerate(value)
            for k, v in sequence:
                try:
                    self.field._validate(v)
                except ValidationError as error:
                    errors[k] = error.errors or error
                except (ValueError, AssertionError) as error:
                    errors[k] = error

            if errors:
                field_class = self.field.__class__.__name__
                self.error('Invalid %s item (%s)' % (field_class, value),
                           errors=errors)
        # Don't allow empty values if required
        if self.required and not value:
            self.error('Field is required and cannot be empty')

    def prepare_query_value(self, op, value):
        return self.to_mongo(value)

    def lookup_member(self, member_name):
        if self.field:
            return self.field.lookup_member(member_name)
        return None

    def _set_owner_document(self, owner_document):
        if self.field:
            self.field.owner_document = owner_document
        self._owner_document = owner_document

    def _get_owner_document(self, owner_document):
        self._owner_document = owner_document

    owner_document = property(_get_owner_document, _set_owner_document)


class ObjectIdField(BaseField):
    """A field wrapper around MongoDB's ObjectIds.
    """

    def to_python(self, value):
        return value

    def to_mongo(self, value):
        if value and not isinstance(value, ObjectId):
            try:
                return ObjectId(str(value))
            except Exception as e:
                # e.message attribute has been deprecated since Python 2.6
                self.error(str(e))
        return value

    def prepare_query_value(self, op, value):
        return self.to_mongo(value)

    def validate(self, value):
        try:
            ObjectId(str(value))
        except:
            self.error('Invalid Object ID')


class GeoJsonBaseField(BaseField):
    """A geo json field storing a geojson style object.
    .. versionadded:: 0.8
    """

    _geo_index = pymongo.GEOSPHERE
    _type = "GeoBase"

    def __init__(self, auto_index=True, *args, **kwargs):
        """
        :param auto_index: Automatically create a "2dsphere" index. Defaults
            to `True`.
        """
        self._name = "%sField" % self._type
        if not auto_index:
            self._geo_index = False
        super(GeoJsonBaseField, self).__init__(*args, **kwargs)

    def validate(self, value):
        """Validate the GeoJson object based on its type
        """
        if isinstance(value, dict):
            if set(value.keys()) == set(['type', 'coordinates']):
                if value['type'] != self._type:
                    self.error('%s type must be "%s"' % (self._name, self._type))
                return self.validate(value['coordinates'])
            else:
                self.error('%s can only accept a valid GeoJson dictionary'
                           ' or lists of (x, y)' % self._name)
                return
        elif not isinstance(value, (list, tuple)):
            self.error('%s can only accept lists of [x, y]' % self._name)
            return

        validate = getattr(self, "_validate_%s" % self._type.lower())
        error = validate(value)
        if error:
            self.error(error)

    def _validate_polygon(self, value):
        if not isinstance(value, (list, tuple)):
            return 'Polygons must contain list of linestrings'

        # Quick and dirty validator
        try:
            value[0][0][0]
        except:
            return "Invalid Polygon must contain at least one valid linestring"

        errors = []
        for val in value:
            error = self._validate_linestring(val, False)
            if not error and val[0] != val[-1]:
                error = 'LineStrings must start and end at the same point'
            if error and error not in errors:
                errors.append(error)
        if errors:
            return "Invalid Polygon:\n%s" % ", ".join(errors)

    def _validate_linestring(self, value, top_level=True):
        """Validates a linestring"""
        if not isinstance(value, (list, tuple)):
            return 'LineStrings must contain list of coordinate pairs'

        # Quick and dirty validator
        try:
            value[0][0]
        except:
            return "Invalid LineString must contain at least one valid point"

        errors = []
        for val in value:
            error = self._validate_point(val)
            if error and error not in errors:
                errors.append(error)
        if errors:
            if top_level:
                return "Invalid LineString:\n%s" % ", ".join(errors)
            else:
                return "%s" % ", ".join(errors)

    def _validate_point(self, value):
        """Validate each set of coords"""
        if not isinstance(value, (list, tuple)):
            return 'Points must be a list of coordinate pairs'
        elif not len(value) == 2:
            return "Value (%s) must be a two-dimensional point" % repr(value)
        elif (not isinstance(value[0], (float, int)) or
              not isinstance(value[1], (float, int))):
            return "Both values (%s) in point must be float or int" % repr(value)

    def to_mongo(self, value):
        if isinstance(value, dict):
            return value
        return SON([("type", self._type), ("coordinates", value)])
