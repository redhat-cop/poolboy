import collections
import copy
import datetime
import jinja2
import json

class TimeStamp(object):
    def __init__(self, set_datetime=None):
        if isinstance(set_datetime, datetime.datetime):
            self.datetime = set_datetime
        elif isinstance(set_datetime, str):
            self.datetime = datetime.datetime.strptime(set_datetime, "%Y-%m-%dT%H:%M:%SZ")
        else:
            self.datetime = set_datetime

    def __call__(self, arg):
        return TimeStamp(arg)

    def __str__(self):
        return self.datetime.strftime('%FT%TZ')

    def add(self, interval):
        if interval.endswith('d'):
            self.datetime = self.datetime + datetime.timedelta(days=int(interval[0:-1]))
        elif interval.endswith('h'):
            self.datetime = self.datetime + datetime.timedelta(hours=int(interval[0:-1]))
        elif interval.endswith('m'):
            self.datetime = self.datetime + datetime.timedelta(minutes=int(interval[0:-1]))
        elif interval.endswith('s'):
            self.datetime = self.datetime + datetime.timedelta(seconds=int(interval[0:-1]))
        else:
            raise Exception("Invalid interval format %s" % (interval))
        return self

    @property
    def utcnow(self):
        return TimeStamp(datetime.datetime.utcnow())

jinja2env = jinja2.Environment(
    block_start_string='{%:',
    block_end_string=':%}',
    comment_start_string='{#:',
    comment_end_string=':#}',
    variable_start_string='{{:',
    variable_end_string=':}}'
)
jinja2env.filters['to_json'] = lambda x: json.dumps(x)

def dict_merge(dct, merge_dct):
    """ Recursive dict merge. Inspired by :meth:``dict.update()``, instead of
    updating only top-level keys, dict_merge recurses down into dicts nested
    to an arbitrary depth, updating keys. The ``merge_dct`` is merged into
    ``dct``.
    :param dct: dict onto which the merge is executed
    :param merge_dct: dct merged into dct
    :return: None
    """
    # FIXME? What about lists within dicts? Such as container lists within a pod?
    for k, v in merge_dct.items():
        if k in dct \
        and isinstance(dct[k], dict) \
        and isinstance(merge_dct[k], collections.Mapping):
            dict_merge(dct[k], merge_dct[k])
        else:
            dct[k] = copy.deepcopy(merge_dct[k])

def defaults_from_schema(schema):
    obj = {}
    for prop, property_schema in schema.get('properties', {}).items():
        if 'default' in property_schema and prop not in obj:
            obj[prop] = property_schema['default']
        if property_schema['type'] == 'object':
            defaults = defaults_from_schema(property_schema)
            if defaults:
                if not prop in obj:
                    obj[prop] = {}
                dict_merge(obj[prop], defaults)
    if obj:
        return obj

def jinja2process(template, variables):
    variables = copy.copy(variables)
    variables['timestamp'] = TimeStamp()
    j2template = jinja2env.from_string(template)
    return j2template.render(variables)

def recursive_process_template_strings(template, variables={}):
    if isinstance(template, dict):
        return { k: recursive_process_template_strings(v, variables) for k, v in template.items() }
    elif isinstance(template, list):
        return [ recursive_process_template_strings(item) for item in template ]
    elif isinstance(template, str):
        return jinja2process(template, variables)
    else:
        return template
