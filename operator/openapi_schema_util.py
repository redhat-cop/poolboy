def defaults_from_schema(schema):
    defaults = {}
    for prop, property_schema in schema.get('properties', {}).items():
        if 'default' in property_schema:
            defaults[prop] = property_schema['default']
        if property_schema.get('type') == 'object':
            property_defaults = defaults_from_schema(property_schema)
            if property_defaults:
                defaults[prop] = property_defaults
    return defaults
