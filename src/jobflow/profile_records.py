"""Answer indexed employment/education fields without leaking between records."""
import calendar
import re
from .applications import normalize_question


def bind_existing_records(values, fields):
    """Match partial employer-autofilled rows before completing missing fields."""
    bindings, anchors, indices = {}, {}, set()
    values['_form_employment_record_bindings'] = bindings
    for field in fields:
        record = re.match(r'employment record (\d+) ', normalize_question(field['question']))
        if record:
            indices.add(int(record[1]) - 1)
        match = re.fullmatch(r'employment record (\d+) (company|job title)', normalize_question(field['question']))
        if match and str(field.get('value') or '').strip():
            anchors.setdefault(int(match[1]) - 1, {})[match[2]] = field['value']
    roles = values.get('employment_history', {}).get('roles', [])
    for index, row in anchors.items():
        choices = [i for i, role in enumerate(roles) if all(
            normalize_question(str(role.get('title' if key == 'job title' else 'company', ''))) == normalize_question(value)
            for key, value in row.items())]
        if len(choices) != 1:
            return f'Existing employment record {index + 1} does not uniquely match saved history; no guessed dates'
        if choices[0] in bindings.values():
            return 'Existing employment rows repeat the same saved role; history needs reconciliation'
        bindings[str(index)] = choices[0]
    available = [i for i in range(len(roles)) if i not in bindings.values()]
    for index in sorted(indices):
        if str(index) not in bindings:
            chosen = (-1 if index >= len(roles) else
                      index if index in available else available[0] if available else -1)
            bindings[str(index)] = chosen
            if chosen in available:
                available.remove(chosen)
    return ''


def record_answer(values, question):
    match = re.fullmatch(r'(employment|education) record (\d+) (.+)', question)
    if not match:
        return None
    kind, number, field = match.groups()
    records = (values.get('employment_history', {}).get('roles', []) if kind == 'employment'
               else [values.get('education', {})])
    index = int(number) - 1
    if kind == 'employment':
        index = values.get('_form_employment_record_bindings', {}).get(str(index), index)
    if not 0 <= index < len(records):
        return None
    record = records[index]
    aliases = {'job title': 'title', 'company': 'company', 'location': 'location',
               'school': 'school', 'degree': 'degree', 'field of study': 'discipline',
               'description': 'description', 'current': 'current'}
    if field in {'start date', 'end date'}:
        prefix = field.split()[0]
        year, month = record.get(prefix + '_year'), record.get(prefix + '_month')
        months = {name.casefold(): i for i, name in enumerate(calendar.month_name) if name}
        month_number = months.get(str(month).casefold())
        return f'{year}-{month_number:02}' if year and month_number else None
    key = aliases.get(field, field.replace(' ', '_'))
    value = record.get(key)
    if value is None:
        return None
    return ('Yes' if value else 'No') if isinstance(value, bool) else str(value)
