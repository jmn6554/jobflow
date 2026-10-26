"""Read public React Router/Turbo Stream loader data without executing scripts."""
import json
import re


class LoaderData:
    def __init__(self, html):
        chunks = re.findall(r'window\.__reactRouterContext\.streamController\.enqueue\(("(?:\\.|[^"\\])*")\)', html)
        if not chunks:
            raise ValueError('React Router loader data missing')
        stream = ''.join(json.loads(chunk) for chunk in chunks)
        self.table = json.loads(stream.splitlines()[0])
        if not isinstance(self.table, list):
            raise ValueError('Malformed React Router data table')
        self.cache = {}
        self.active = set()

    def decode(self, index, depth=0):
        if type(index) is not int or index >= len(self.table) or depth > 100:
            raise ValueError('Invalid React Router data reference')
        if index < 0:
            if index in {-1, -5}:  # undefined/null sentinels
                return None
            raise ValueError('Unsupported React Router sentinel')
        if index in self.active:
            raise ValueError('Cyclic React Router data')
        if index in self.cache:
            return self.cache[index]
        value = self.table[index]
        self.active.add(index)
        try:
            if isinstance(value, dict):
                result = {}
                for key, ref in value.items():
                    if not re.fullmatch(r'_\d+', key):
                        raise ValueError('Malformed React Router object key')
                    name = self.decode(int(key[1:]), depth + 1)
                    if not isinstance(name, str):
                        raise ValueError('React Router object key must be text')
                    result[name] = self.decode(ref, depth + 1)
            elif isinstance(value, list):
                if value and isinstance(value[0], str):
                    if value[0] == 'Date' and len(value) == 2 and isinstance(value[1], str):
                        result = value[1]
                    else:
                        raise ValueError('Unsupported React Router tagged value')
                else:
                    result = [self.decode(ref, depth + 1) for ref in value]
            else:
                result = value
            self.cache[index] = result
            return result
        finally:
            self.active.remove(index)

    def field(self, name):
        indices = [i for i, value in enumerate(self.table) if value == name]
        refs = {obj['_' + str(i)] for obj in self.table if isinstance(obj, dict)
                for i in indices if '_' + str(i) in obj}
        if len(refs) != 1:
            raise ValueError('Loader field missing or ambiguous: ' + name)
        return self.decode(next(iter(refs)))
