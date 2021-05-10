import collections
import copy
import datetime
import jinja2
import json

class TimeDelta(object):
    def __init__(self, set_timedelta=None):
        if isinstance(set_timedelta, datetime.timedelta):
            self.timedelta = set_timedelta
        elif isinstance(set_timedelta, str):
            if set_timedelta.endswith('d'):
                self.timedelta = datetime.timedelta(days=int(set_timedelta[0:-1]))
            elif set_timedelta.endswith('h'):
                self.timedelta = datetime.timedelta(hours=int(set_timedelta[0:-1]))
            elif set_timedelta.endswith('m'):
                self.timedelta = datetime.timedelta(minutes=int(set_timedelta[0:-1]))
            elif set_timedelta.endswith('s'):
                self.timedelta = datetime.timedelta(seconds=int(set_timedelta[0:-1]))
            else:
                raise Exception("Invalid interval format {}".format(set_timedelta))
        elif set_timedelta:
            raise Exception("Invalid type for time interval format {}".format(type(set_timedelta).__name__))
        else:
            self.timedelta = datetime.timedelta()

    def __call__(self, arg):
        return TimeDelta(arg)

    def __eq__(self, other):
        return self.timedelta == other.timedelta

    def __ne__(self, other):
        return self.timedelta != other.timedelta

    def __ge__(self, other):
        return self.timedelta >= other.timedelta

    def __gt__(self, other):
        return self.timedelta > other.timedelta

    def __le__(self, other):
        return self.timedelta <= other.timedelta

    def __lt__(self, other):
        return self.timedelta < other.timedelta

    def __str__(self):
        seconds = self.timedelta.total_seconds()
        if seconds == 0:
            return "0s"
        elif seconds % 86400 == 0:
            return f"{int(seconds / 86400)}d"
        elif seconds % 3600 == 0:
            return f"{int(seconds / 3600)}h"
        elif seconds % 60 == 0:
            return f"{int(seconds / 60)}m"
        else:
            return f"{int(seconds)}s"

class TimeStamp(object):
    def __init__(self, set_datetime=None):
        if not set_datetime:
            self.datetime = datetime.datetime.utcnow()
        elif isinstance(set_datetime, datetime.datetime):
            self.datetime = set_datetime
        elif isinstance(set_datetime, str):
            self.datetime = datetime.datetime.strptime(set_datetime, "%Y-%m-%dT%H:%M:%SZ")
        else:
            self.datetime = set_datetime

    def __call__(self, arg):
        return TimeStamp(arg)

    def __eq__(self, other):
        return self.datetime == other.datetime

    def __ne__(self, other):
        return self.datetime != other.datetime

    def __ge__(self, other):
        return self.datetime >= other.datetime

    def __gt__(self, other):
        return self.datetime > other.datetime

    def __le__(self, other):
        return self.datetime <= other.datetime

    def __lt__(self, other):
        return self.datetime < other.datetime

    def __str__(self):
        return self.datetime.strftime('%FT%TZ')

    def __add__(self, interval):
        return self.add(interval)

    def add(self, timedelta):
        ret = TimeStamp(self.datetime)
        if isinstance(timedelta, TimeDelta):
            ret.datetime += timedelta.timedelta
        elif isinstance(timedelta, datetime.timedelta):
            ret.datetime += timedelta
        else:
            ret.datetime += TimeDelta(timedelta).timedelta
        return ret

    @property
    def utcnow(self):
        return TimeStamp()

jinja2envs = {
    'jinja2': jinja2.Environment(),
    'legacy': jinja2.Environment(
        block_start_string='{%:',
        block_end_string=':%}',
        comment_start_string='{#:',
        comment_end_string=':#}',
        variable_start_string='{{:',
        variable_end_string=':}}',
    ),
}
jinja2envs['jinja2'].filters['to_json'] = lambda x: json.dumps(x)
jinja2envs['legacy'].filters['to_json'] = lambda x: json.dumps(x)

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

def jinja2process(template, template_style, variables):
    variables = copy.copy(variables)
    variables['timedelta'] = TimeDelta()
    variables['timestamp'] = TimeStamp()
    jinja2env = jinja2envs.get(template_style)
    j2template = jinja2env.from_string(template)
    return j2template.render(variables)

def recursive_process_template_strings(template, template_style, variables={}):
    if isinstance(template, dict):
        return { k: recursive_process_template_strings(v, template_style, variables) for k, v in template.items() }
    elif isinstance(template, list):
        return [ recursive_process_template_strings(item, template_style, variables) for item in template ]
    elif isinstance(template, str):
        return jinja2process(template, template_style, variables)
    else:
        return template
