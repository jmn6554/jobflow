"""Shared DOM interpretation for required questions, not hidden widget plumbing."""

QUESTION_HELPERS = r'''
const drawn = el => !!el.getClientRects().length && getComputedStyle(el).visibility !== 'hidden';
const visible = el => !el.disabled && !el.closest('[hidden],[aria-hidden="true"]') &&
    (drawn(el) || (['radio','checkbox'].includes(el.type) &&
        [...(el.labels || [])].some(label => !label.closest('[hidden],[aria-hidden="true"]') && drawn(label))));
// A section-wide fieldset may contain many questions. Prefer a bounded
// single-question wrapper with its own direct label over that broad legend.
const structuralGroup = el => {
    if (el.type !== 'radio' || !el.name) return null;
    for (let p = el.parentElement, depth = 0; p && depth < 7 && !p.matches('fieldset,form,body'); p = p.parentElement, depth++) {
        const fields = [...p.querySelectorAll('input:not([type=hidden]),select,textarea')];
        if (!fields.length || fields.some(c => c.type !== 'radio' || c.name !== el.name)) continue;
        const prompts = [...p.querySelectorAll(':scope > label')].filter(label =>
            !fields.some(c => c.id && label.htmlFor === c.id) && !!label.textContent.trim());
        if (prompts.length === 1) return p;
    }
    return null;
};
const groupOf = el => el.closest('[data-automation-id^=formField]') ||
    el.closest('.chakra-form-control,.MuiFormControl-root') ||
    structuralGroup(el) ||
    el.closest('.application-question,.ashby-application-form-field-entry,fieldset,[role=radiogroup]');
// Fabric custom selects expose a visible button and a hidden native select.
// Only treat that paired structure as a field, never a general action menu.
const pairedSelect = el => el.matches('button[aria-haspopup="true"]') ?
    el.closest('.fab-Select,[data-fabric-component="Select"]')?.querySelector('select') : null;
const select2Native = el => el.matches('.select2-selection[role=combobox]') &&
    el.closest('.select2-container')?.parentElement?.querySelectorAll('select').length === 1 ?
    el.closest('.select2-container').parentElement.querySelector('select') : null;
// Custom Ashby-backed forms keep typed location text separate from the
// committed structured value. Empty hidden value means no selection yet.
const locationValue = el => el.name === '_systemfield_location_label' ?
    el.parentElement?.querySelector('input[type="hidden"][name="_systemfield_location_location_value"]') : null;
const labelText = label => {
    if (!label) return '';
    // A wrapping label may contain the select itself; its options are not
    // part of the question. Remove only controls, preserving prompt text.
    if (!label.querySelector('select,textarea,button')) return label.innerText;
    const copy = label.cloneNode(true);
    copy.querySelectorAll('input,select,textarea,button').forEach(node=>node.remove());
    return copy.textContent;
};
const checkboxChoices = el => {
    if (el.type !== 'checkbox') return [];
    const group = groupOf(el);
    if (group?.matches('.ashby-application-form-input-checkbox-group'))
        return [...group.querySelectorAll('input[type=checkbox]')].filter(visible);
    if (group?.matches('.application-question') && group.querySelector('.application-label.multiple-select'))
        return [...group.querySelectorAll('input[type=checkbox]')].filter(c => visible(c) && c.name === el.name);
    // Greenhouse groups choices in a required fieldset but marks every
    // individual checkbox required. It means one group answer, not all boxes.
    if (group?.matches('fieldset.checkbox[aria-required="true"]') && el.name.endsWith('[]') &&
        group.querySelector(':scope > legend'))
        return [...group.querySelectorAll('input[type=checkbox]')].filter(c => visible(c) && c.name === el.name);
    const prompt = group?.querySelector('legend,.application-label,[class*=question-title]')?.innerText || '';
    return /select all|check all|choose all/i.test(prompt) ? [...group.querySelectorAll('input[type=checkbox]')].filter(visible) : [];
};
const recordLabel = (el, label) => {
    // Only numbered employment/education sections establish record identity.
    // Never reuse the current employer for an unrecognized later record.
    let record = null;
    for (let p = el.parentElement, depth = 0; p && depth < 12; p = p.parentElement, depth++) {
        const heads = [...p.querySelectorAll('h1,h2,h3,h4,h5,h6,legend,[role=heading],[data-automation-id="panel-set-heading"]')]
            .map(h => h.textContent.trim().match(/^(Work Experience|Employment|Education)\s+(\d+)$/i)).filter(Boolean);
        if (heads.length === 1) { record = heads[0]; break; }
        if (heads.length > 1) break;
    }
    if (!record) return label;
    const kind = record[1].toLowerCase() === 'education' ? 'education' : 'employment';
    const clean = text => text.toLowerCase().replace(/[✱*]/g, '').replace(/\s+/g, ' ').trim();
    const raw = clean(label);
    const aliases = {'job title':'job title', 'company':'company', 'employer':'company', 'location':'location',
        'role description':'description', 'description':'description', 'i currently work here':'current',
        'school or university':'school', 'school':'school', 'university':'school', 'degree':'degree', 'field of study':'field of study'};
    let field = aliases[raw];
    const groupLabel = clean(groupOf(el)?.querySelector('label')?.textContent || '');
    const dateKind = /^(from|start date)$/.test(groupLabel) ? 'start' : /^(to|to \(actual or expected\)|end date)$/.test(groupLabel) ? 'end' : null;
    if (dateKind) {
        const segment = el.getAttribute('data-automation-id') || '';
        field = dateKind + (segment.includes('Month') ? ' month' : segment.includes('Year') ? ' year' : ' date');
    }
    if (!field) return label;
    return `${kind} record ${record[2]} ${field}`;
};
const labelOf = el => {
    if (el.type === 'radio') {
        const semantic = {'gender':'Gender', 'race_ethnicity':'Race/ethnicity', 'eeoc.veteran_status':'Veteran status', 'veteran_status':'Veteran status'};
        if (semantic[el.name]) return semantic[el.name];
        // Some phone components place SMS consent radios beneath the Phone
        // heading. Their explicit choice labels identify the separate consent.
        const peers = el.name ? [...document.querySelectorAll('input[type=radio]')]
            .filter(c => c.name === el.name && visible(c)) : [];
        const labels = peers.map(c => c.labels?.[0]?.innerText || c.closest('label')?.innerText || '');
        if (labels.length === 2 && labels.every(text => /consent.*(?:text messages|sms|WhatsApp messages)/i.test(text)) &&
            labels.some(text => /^\s*yes\b/i.test(text)) && labels.some(text => /^\s*no\b/i.test(text)))
            return labels.every(text => /WhatsApp/i.test(text)) ? 'Do you consent to receiving WhatsApp messages?' : 'Do you consent to receiving recruiting text messages?';
    }
    if (el.name === 'eeo[disabilitySignatureDate]') return 'Disability disclosure signature date';
    const employmentLabels = {'company-name-0':'Current company', 'title-0':'Current job title',
        'start-date-month-0':'Current employment start month', 'start-date-year-0':'Current employment start year',
        'end-date-month-0':'Current employment end month', 'end-date-year-0':'Current employment end year',
        'current-role-0_1':'Is this your current role?'};
    if (employmentLabels[el.id]) return employmentLabels[el.id];
    if (el.id === 'country' && el.getAttribute('role') === 'combobox' && document.getElementById('phone'))
        return 'Phone country code';
    if (el.getAttribute('role') === 'combobox' && el.closest('[data-testid="phone_number-code"]'))
        return 'Phone country code';
    const group = groupOf(el);
    if (el.type === 'radio' && group && group === structuralGroup(el))
        return group.querySelector(':scope > label').textContent.trim();
    // Workday may label the checkbox only "I Consent" while placing the
    // actual declaration immediately above its form field. Keep that scoped
    // text; never borrow privacy approval from another section of the page.
    if (el.type === 'checkbox' && group?.getAttribute('data-automation-id') === 'formField-acceptTermsAndAgreements') {
        const section = group.parentElement;
        const fields = section?.querySelectorAll('input,select,textarea') || [];
        const context = section?.innerText?.trim() || '';
        if (fields.length === 1 && context.length > group.innerText.trim().length && context.length < 10000)
            return context;
    }
    if (el.type === 'radio' && group) {
        const groupLabel = (group.getAttribute('aria-labelledby') || '').split(/\s+/)
            .map(id => document.getElementById(id)?.innerText || '').join(' ').trim();
        if (groupLabel) return groupLabel;
        if (group.getAttribute('aria-label')) return group.getAttribute('aria-label');
    }
    const prompt = group?.querySelector('.application-label,legend,[class*=question-title],.chakra-form__label,.MuiFormLabel-root');
    if (pairedSelect(el)) {
        const native = pairedSelect(el);
        return (native.labels?.[0]?.innerText || prompt?.innerText || '').trim();
    }
    if (checkboxChoices(el).length > 1) return prompt.innerText.trim();
    // A one-choice checkbox group still has a question. Its leaf label
    // (often just "Yes") is the answer, not the declaration being accepted.
    if (el.type === 'checkbox' && prompt && group?.matches('.application-question') &&
        group.querySelectorAll('input,select,textarea').length === 1)
        return prompt.innerText.trim();
    const explicit = el.id && document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
    const labelled = (el.getAttribute('aria-labelledby') || '').split(/\s+/)
        .map(id => document.getElementById(id)?.innerText || '').join(' ').trim();
    // Rippling custom questions put the prompt in a paragraph immediately
    // above a single field component, without linking it with label/for.
    // Require that structure; never use a broad ancestor's mixed questions.
    let componentPrompt = '';
    if (el.closest('[data-testid^="customQuestions."]')) {
        const field = el.closest('[data-testid="field"]');
        const wrapper = field?.parentElement;
        const prompts = wrapper ? [...wrapper.querySelectorAll('p')].filter(p => !field.contains(p) && visible(p)) : [];
        if (wrapper?.querySelectorAll('input,select,textarea').length === 1 && prompts.length === 1)
            componentPrompt = prompts[0].innerText.trim();
    }
    const placeholder = el.getAttribute('placeholder') || '';
    const usefulPlaceholder = /^(?:full name|first name|last name|email address|phone number|address|desired salary)$/i.test(placeholder.trim()) ? placeholder : '';
    // Some accessible-component libraries render a label beside a wrapped
    // input, but omit the input id. Use only an unambiguous one-control wrapper.
    let nearbyLabel = '';
    if (!['radio','checkbox'].includes(el.type)) {
        for (let parent = el.parentElement, depth = 0; parent && depth < 4; parent = parent.parentElement, depth++) {
            if (parent.querySelectorAll('input,select,textarea').length !== 1) break;
            const labels = [...parent.querySelectorAll('label')].filter(visible);
            if (labels.length === 1) { nearbyLabel = labels[0].innerText; break; }
        }
    }
    const ariaLabel = el.getAttribute('aria-label') || '';
    const meaningfulAria = /^\s*(select one|select|choose|search|textbox)(\s+required)?\s*$/i.test(ariaLabel) ? '' : ariaLabel;
    const ownLabel = labelText(explicit) || labelled || meaningfulAria || labelText(el.closest('label')) || nearbyLabel || componentPrompt;
    return recordLabel(el, ((el.type === 'radio' ? prompt?.innerText || ownLabel : ownLabel || usefulPlaceholder || prompt?.innerText)
        || el.name || el.id || '').trim());
};
const controls = [...document.querySelectorAll('input,select,textarea,button[aria-haspopup="listbox"],button[aria-haspopup="true"],.select2-selection[role=combobox]')]
    .filter(el => visible(el) && (pairedSelect(el) || el.getAttribute('aria-haspopup') === 'listbox' || !['hidden','submit','button'].includes(el.type)));
const markedRequired = el => {
    const labels = [...(el.labels || [])];
    const group = structuralGroup(el);
    if (group) labels.push(group.querySelector(':scope > label'));
    return labels.some(label => [label, ...label.querySelectorAll('[class]')].some(node =>
        [...node.classList].some(token => /(?:^|[-_])required(?:$|[-_])/.test(token) && !/(?:not|non|optional)[-_]/.test(token))));
};
const required = el => el.required || pairedSelect(el)?.required || el.getAttribute('aria-required') === 'true' || markedRequired(el) ||
    !!groupOf(el)?.querySelector('label.ashby-application-form-question-title[class*="_required_"]') ||
    /[✱*]/.test(labelOf(el)) || (/^(employment|education) record \d+ /.test(labelOf(el)) &&
        /[✱*]/.test(el.labels?.[0]?.textContent || groupOf(el)?.querySelector('label')?.textContent || ''));
const workdaySelected = el => el.hasAttribute('data-uxi-multiselect-id') &&
    [...(el.closest('[data-automation-id="multiSelectContainer"]')?.querySelectorAll('[data-automation-id="selectedItem"]') || [])]
        .some(item => visible(item) && !!item.innerText.trim());
const selected = el => {
    if (el.tagName === 'SELECT') return [...el.selectedOptions].some(o =>
        !!o.value.trim() && !/^[-*\s]*(?:please\s+)?(?:select|choose)(?:\b|\.{3})/i.test(o.text.trim()));
    if (select2Native(el)) return !!select2Native(el).value.trim();
    if (pairedSelect(el)) return !!pairedSelect(el).value.trim();
    if (locationValue(el)) return !!el.value.trim() && !!locationValue(el).value.trim();
    if (el.hasAttribute('data-uxi-multiselect-id')) return workdaySelected(el);
    if (el.tagName === 'BUTTON' && el.getAttribute('aria-haspopup') === 'listbox')
        return !!el.innerText.trim() && !/^[-\s]*(?:select|choose)(?:\b|\.{3})/i.test(el.innerText.trim());
    if (el.type === 'checkbox') return checkboxChoices(el).length > 1 ? checkboxChoices(el).some(c=>c.checked) : el.checked;
    if (el.type === 'radio') {
        if (el.name) return controls.some(c => c.type === 'radio' && c.name === el.name && c.checked);
        const group = groupOf(el);
        if (group) return [...group.querySelectorAll(`input[type="${el.type}"]`)].some(c => visible(c) && c.checked);
        if (el.type === 'radio' && el.name) return controls.some(c => c.name === el.name && c.checked);
        return el.checked;
    }
    if (el.type === 'file') return !!el.files?.length;
    if (el.getAttribute('role') === 'combobox') {
        const react = el.closest('.react-select');
        if (react && react.querySelectorAll('input[role=combobox]').length === 1) {
            const values = [...react.querySelectorAll('input[type=hidden][name]')];
            return !!react.querySelector('.react-select__single-value,.react-select__multi-value') &&
                (!values.length || values.some(input => !!input.value.trim()));
        }
        // SuccessFactors copies a committed picklist choice into title and
        // clears aria-invalid. Typed search text alone does neither.
        if (el.matches('.rcmpaginatedselectinput'))
            return !!el.value.trim() && el.title === el.value &&
                el.getAttribute('aria-invalid') !== 'true' && el.value !== 'No Selection';
        // Ashby's autocomplete has no React Select value chip. Count only a
        // value observed after our real option click, not arbitrary typed text.
        if (el.matches('.ashby-application-form-input-autocomplete,.rcmpaginatedselectinput'))
            return el.getAttribute('aria-expanded') === 'false' && !!el.value.trim() &&
                window.__jobflowObservedSelections?.get(el) === el.value.trim();
        // Rippling phone selectors retain the selected dial code and ISO
        // country in the input, not in a React Select singleValue element.
        if (el.closest('[data-testid="phone_number-code"]'))
            return el.getAttribute('aria-expanded') === 'false' && /^\+\d{1,4}\s+[A-Z]{2}$/.test(el.value.trim());
        // React Select search text is not a committed selection.
        const shell = el.closest('[class*=container]')?.closest('.select-shell') || el.closest('.select-shell');
        return !!shell?.querySelector('[class*=singleValue],[class*=single-value],[class*=multiValue],[class*=multi-value]');
    }
    return !!el.value?.trim() && !/^[-\s]*(?:select|choose)(?:\b|\.{3})/i.test(el.value.trim());
};
const choicesOf = el => select2Native(el) ? [...select2Native(el).options].filter(o=>o.value).map(o=>o.text) :
    pairedSelect(el) ? [...pairedSelect(el).options].filter(o=>o.value).map(o=>o.text) :
    el.tagName === 'SELECT' ? [...el.options].filter(o=>o.value).map(o=>o.text) :
    el.type === 'checkbox' && checkboxChoices(el).length <= 1 ? [labelOf(el)] :
    (!['radio','checkbox'].includes(el.type) ? [] :
    (el.type === 'radio' && el.name ? controls.filter(c=>c.type==='radio' && c.name===el.name) :
    (checkboxChoices(el).length > 1 ? checkboxChoices(el) : [el]))
        .filter(visible).map(c => c.labels?.[0]?.innerText || c.closest('label')?.innerText || c.value));
const selectedAnswer = el => {
    if (!selected(el)) return '';
    if (pairedSelect(el)) return [...pairedSelect(el).selectedOptions].map(o=>o.text).join(' / ');
    if (checkboxChoices(el).length > 1) return checkboxChoices(el).filter(c=>c.checked)
        .map(c=>c.labels?.[0]?.innerText || c.value).join(' / ');
    if (el.tagName === 'SELECT') return [...el.selectedOptions].map(o=>o.text).join(' / ');
    if (el.type === 'radio') {
        const chosen = controls.find(c=>c.type==='radio' && c.name===el.name && c.checked);
        return chosen?.labels?.[0]?.innerText || chosen?.closest('label')?.innerText || '';
    }
    if (el.getAttribute('aria-haspopup') === 'listbox') return el.innerText.trim();
    return '';
};
const ashbyChoices = [...document.querySelectorAll('label.ashby-application-form-question-title')]
    .filter(l => visible(l) && l.className.includes('_required_'))
    .map(l => {
        const group = l.parentElement;
        const buttons = [...group.querySelectorAll('button[data-option]')];
        const radios = [...group.querySelectorAll('input[type=radio]')];
        if (!buttons.length && !radios.length) return null;
        const sms = radios.some(r=>/text messages/i.test(r.labels?.[0]?.innerText || ''));
        return {id:l.htmlFor, question:sms?'Do you consent to receiving recruiting text messages?':l.innerText.trim(), kind:buttons.length?'yesno':'radio',
            selected:buttons.some(b=>b.getAttribute('aria-pressed')==='true') || radios.some(r=>r.checked),
            options:buttons.length?buttons.map(b=>b.innerText):radios.map(r=>r.labels?.[0]?.innerText || r.value)};
    }).filter(Boolean);
'''

REQUIRED_QUESTIONS = '() => {' + QUESTION_HELPERS + r'''
return controls.filter(el => required(el) && (checkboxChoices(el).length < 2 || checkboxChoices(el)[0] === el)).map(el => ({id: el.id, question: labelOf(el), kind: el.type, role: pairedSelect(el) || el.getAttribute('aria-haspopup') === 'listbox' ? 'combobox' : el.getAttribute('role'),
    value: ['INPUT','TEXTAREA'].includes(el.tagName) && !['password','file'].includes(el.type) ? el.value : '',
    selected: selected(el), selectedAnswer: selectedAnswer(el), options: choicesOf(el)})).concat(ashbyChoices);
}'''

MISSING_QUESTIONS = '() => {' + QUESTION_HELPERS + r'''
const missing = controls.filter(el => (required(el) && !selected(el)) ||
    el.getAttribute('aria-invalid') === 'true' || (el.validity?.valid === false &&
        !(['checkbox','radio'].includes(el.type) && selected(el)) &&
        !(el.validity.valueMissing && workdaySelected(el))))
    .map(el => (labelOf(el) || 'Unlabelled required field') +
        (choicesOf(el).length ? ' [options: ' + choicesOf(el).join(' / ') + ']' : ''));
return [...new Set(missing.concat(ashbyChoices.filter(q=>!q.selected)
    .map(q=>q.question+' [options: '+q.options.join(' / ')+']')))];
}'''
