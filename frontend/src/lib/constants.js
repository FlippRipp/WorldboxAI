export const WS_URL = `${location.protocol === 'https:' ? 'wss:' : 'ws:'}//${location.host}/ws/chat`;

export const PERSPECTIVE_OPTIONS = [
  { value: 'second_person', label: 'Second person ("you")' },
  { value: 'first_person', label: 'First person ("I")' },
  { value: 'third_person', label: 'Third person ("he/she/they")' },
];

export const TENSE_OPTIONS = [
  { value: 'present', label: 'Present tense' },
  { value: 'past', label: 'Past tense' },
];

export function labelForPerspective(val) {
  return PERSPECTIVE_OPTIONS.find((o) => o.value === val)?.label || 'Second person ("you")';
}

export function labelForTense(val) {
  return TENSE_OPTIONS.find((o) => o.value === val)?.label || 'Present tense';
}
