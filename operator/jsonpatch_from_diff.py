import re

def filter_patch_item(item, update_filters):
    if not update_filters:
        return True
    path = item['path']
    op = item['op']
    for f in update_filters:
        allowed_ops = f.get('allowedOps', ['add','remove','replace'])
        if re.match(f['pathMatch'] + '$', path):
            if op not in allowed_ops:
                return False
            return True
    return False

def jsonpatch_from_diff(a, b, update_filters=None):
    patch = [ item for item in _jsonpatch_from_diff(a, b, []) ]
    if update_filters:
        return [ item for item in patch if filter_patch_item(item, update_filters) ]
    return patch


def _jsonpatch_path(*path):
    return '/' + '/'.join([
        p.replace('~', '~0').replace('/', '~1') for p in path
    ])

def _jsonpatch_from_diff(a, b, path):
    if isinstance(a, dict) and isinstance(b, dict):
        for op in _jsonpatch_from_dict_diff(a, b, path):
            yield op
    elif isinstance(a, list) and isinstance(b, list):
        for op in _jsonpatch_from_list_diff(a, b, path):
            yield op
    elif a != b:
        yield dict(op='replace', path=_jsonpatch_path(*path), value=b)

def _jsonpatch_from_dict_diff(a, b, path):
    for k, v in a.items():
        if k in b:
            for op in _jsonpatch_from_diff(v, b[k], path + [k]):
                yield op
        else:
            yield dict(op='remove', path=_jsonpatch_path(*path, k))
    for k, v in b.items():
        if k not in a:
            yield dict(op='add', path=_jsonpatch_path(*path, k), value=v)

def _jsonpatch_from_list_diff(a, b, path):
    for i in range(min(len(a), len(b))):
        for op in _jsonpatch_from_diff(a[i], b[i], path + [str(i)]):
            yield op
    for i in range(len(a) - 1, len(b) -1, -1):
        yield dict(op='remove', path=_jsonpatch_path(*path, str(i)))
    for i in range(len(a), len(b)):
        yield dict(op='add', path=_jsonpatch_path(*path, str(i)), value=b[i])
