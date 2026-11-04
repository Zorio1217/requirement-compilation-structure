"""Lower fully specified ordinary columns without a model; defer prose constraints."""
import re
import sqlite3

TYPES = {'string': 'TEXT', 'uuid': 'TEXT', 'date': 'TEXT', 'datetime': 'TEXT',
    'json': 'TEXT', 'integer': 'INTEGER', 'boolean': 'INTEGER', 'number': 'REAL'}


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z_][A-Za-z_0-9]*', value):
        raise ValueError('Nonstandard SQL identifier requires model review')
    return '"' + value + '"'


def lower_table(name, entity):
    """Return None rather than silently dropping an untranslatable domain constraint."""
    try:
        table = identifier(name)
        fields = entity['fields']
        if not fields or sum(bool(f['primary_key']) for f in fields.values()) > 1:
            return None
        columns = []
        for field, record in fields.items():
            if record['constraints']:
                return None  # Arbitrary natural-language rules remain model owned.
            quoted = identifier(field)
            column = quoted + ' ' + TYPES[record['type']]
            if record['primary_key']: column += ' PRIMARY KEY'
            if not record['nullable']: column += ' NOT NULL'
            if record['unique'] and not record['primary_key']: column += ' UNIQUE'
            if record['references']:
                target, target_field = record['references'].split('.')
                column += f' REFERENCES {identifier(target)} ({identifier(target_field)})'
            if record['type'] == 'boolean': column += f' CHECK ({quoted} IN (0, 1))'
            if record['type'] == 'json': column += f' CHECK (json_valid({quoted}))'
            columns.append(column)
        sql = f'CREATE TABLE IF NOT EXISTS {table} (\n  ' + ',\n  '.join(columns) + '\n);\n'
        with sqlite3.connect(':memory:') as database: database.executescript(sql)
        return sql
    except (KeyError, TypeError, ValueError, sqlite3.Error):
        return None
