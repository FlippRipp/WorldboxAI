import { useEffect, useCallback, useRef } from 'react';

export function AutoTextarea({ value, onChange, disabled, minRows = 2 }) {
  const ref = useRef(null);

  const adjustHeight = useCallback(() => {
    const el = ref.current;
    if (!el) return;
    el.style.height = 'auto';
    el.style.height = Math.max(el.scrollHeight, minRows * 24) + 'px';
  }, [minRows]);

  useEffect(() => { adjustHeight(); }, [value, disabled, adjustHeight]);

  return (
    <textarea
      ref={ref}
      value={value || ''}
      onChange={onChange}
      onInput={adjustHeight}
      disabled={disabled}
      rows={minRows}
      className="w-full bg-gray-800 border border-gray-600 rounded px-3 py-2 text-gray-200 focus:border-purple-500 focus:outline-none resize-none overflow-hidden whitespace-pre-wrap break-words"
    />
  );
}

export function StepField({ fieldKey, fieldSchema, value, onChange, disabled, onRerollItem, rerollingKey }) {
  const type = fieldSchema?.type || 'string';

  if (type === 'slider') {
    return (
      <div className="flex items-center gap-3">
        <input
          type="range"
          min={fieldSchema.min || 1}
          max={fieldSchema.max || 10}
          value={value ?? fieldSchema.min ?? 1}
          onChange={(e) => onChange(fieldKey, parseInt(e.target.value))}
          disabled={disabled}
          className="flex-1 accent-purple-500"
        />
        <span className="text-purple-300 font-mono w-6 text-right">{value}</span>
      </div>
    );
  }

  if (type === 'number') {
    const min = fieldSchema.min ?? 30;
    const max = fieldSchema.max ?? 500;
    const step = fieldSchema.step ?? 10;
    return (
      <div className="flex items-center gap-3">
        <input
          type="range"
          min={min}
          max={max}
          step={step}
          value={value ?? fieldSchema.default ?? min}
          onChange={(e) => onChange(fieldKey, parseInt(e.target.value))}
          disabled={disabled}
          className="flex-1 accent-purple-500"
        />
        <input
          type="number"
          min={min}
          max={max}
          value={value ?? fieldSchema.default ?? min}
          onChange={(e) => {
            const v = Math.max(min, Math.min(max, parseInt(e.target.value) || min));
            onChange(fieldKey, v);
          }}
          disabled={disabled}
          className="w-20 bg-gray-800 border border-gray-600 rounded px-2 py-1 text-center text-gray-200 focus:border-purple-500 focus:outline-none"
        />
      </div>
    );
  }

  if (type === 'select') {
    return (
      <select
        value={value || (fieldSchema.options?.[0] ?? '')}
        onChange={(e) => onChange(fieldKey, e.target.value)}
        disabled={disabled}
        className="w-full bg-gray-800 border border-gray-600 rounded px-3 py-1.5 text-gray-200 focus:border-purple-500 focus:outline-none"
      >
        {(fieldSchema.options || []).map((opt) => (
          <option key={opt} value={opt}>{opt.charAt(0).toUpperCase() + opt.slice(1)}</option>
        ))}
      </select>
    );
  }

  if (type === 'boolean') {
    return (
      <label className="flex items-center gap-2 cursor-pointer">
        <input
          type="checkbox"
          checked={!!value}
          onChange={(e) => onChange(fieldKey, e.target.checked)}
          disabled={disabled}
          className="accent-purple-500"
        />
        <span className="text-sm text-gray-400">{value ? 'Yes' : 'No'}</span>
      </label>
    );
  }

  if (type === 'text') {
    return (
      <AutoTextarea
        value={value || ''}
        onChange={(e) => onChange(fieldKey, e.target.value)}
        disabled={disabled}
        minRows={3}
      />
    );
  }

  if (type === 'list' && fieldSchema.item_type === 'string') {
    const items = Array.isArray(value) ? value : [];
    const canReroll = fieldSchema.rerollable && typeof onRerollItem === 'function';
    return (
      <div className="space-y-1.5">
        {items.map((item, i) => {
          const thisKey = `${fieldKey}:${i}`;
          const isRerolling = rerollingKey === thisKey;
          return (
            <div key={i} className="flex gap-2 items-start">
              <AutoTextarea
                value={item}
                onChange={(e) => {
                  const next = [...items];
                  next[i] = e.target.value;
                  onChange(fieldKey, next);
                }}
                disabled={disabled || isRerolling}
                minRows={1}
              />
              {canReroll && (
                <button
                  onClick={() => onRerollItem(fieldKey, i)}
                  disabled={!!rerollingKey}
                  title="Reroll this entry with AI"
                  className="text-purple-400 hover:text-purple-300 disabled:opacity-40 px-2 pt-1"
                >
                  {isRerolling ? (
                    <span className="inline-block w-3.5 h-3.5 border-2 border-purple-400/30 border-t-purple-400 rounded-full animate-spin" />
                  ) : (
                    '🔄'
                  )}
                </button>
              )}
              {!disabled && (
                <button
                  onClick={() => onChange(fieldKey, items.filter((_, idx) => idx !== i))}
                  className="text-red-400 hover:text-red-300 px-2 pt-1"
                >
                  ×
                </button>
              )}
            </div>
          );
        })}
        {!disabled && (
          <button
            onClick={() => onChange(fieldKey, [...items, ''])}
            className="text-sm text-purple-400 hover:text-purple-300"
          >
            + Add item
          </button>
        )}
      </div>
    );
  }

  if (type === 'list' && fieldSchema.item_schema) {
    const items = Array.isArray(value) ? value : [];
    const schema = fieldSchema.item_schema;
    return (
      <div className="space-y-3">
        {items.map((item, i) => (
          <div key={i} className="p-3 bg-gray-800/50 border border-gray-700 rounded space-y-2">
            {Object.keys(schema).map((subKey) => {
              const sub = schema[subKey];
              const subType = typeof sub === 'object' ? sub.type : sub;
              if (subType === 'list') {
                const subItems = Array.isArray(item?.[subKey]) ? item[subKey] : [];
                return (
                  <div key={subKey}>
                    <label className="text-xs text-gray-500">{typeof sub === 'object' ? sub.label : subKey}</label>
                    <div className="space-y-1">
                      {subItems.map((subItem, j) => (
                        <div key={j} className="flex gap-1 items-start">
                          <AutoTextarea
                            value={subItem}
                            onChange={(e) => {
                              const nextItems = [...items];
                              const nextSub = [...subItems];
                              nextSub[j] = e.target.value;
                              nextItems[i] = { ...nextItems[i], [subKey]: nextSub };
                              onChange(fieldKey, nextItems);
                            }}
                            disabled={disabled}
                            minRows={1}
                          />
                          {!disabled && (
                            <button
                              onClick={() => {
                                const nextItems = [...items];
                                nextItems[i] = { ...nextItems[i], [subKey]: subItems.filter((_, idx) => idx !== j) };
                                onChange(fieldKey, nextItems);
                              }}
                              className="text-red-400 hover:text-red-300 px-1 text-sm pt-0.5"
                            >
                              ×
                            </button>
                          )}
                        </div>
                      ))}
                      {!disabled && (
                        <button onClick={() => {
                          const nextItems = [...items];
                          const nextSub = [...subItems, ''];
                          nextItems[i] = { ...nextItems[i], [subKey]: nextSub };
                          onChange(fieldKey, nextItems);
                        }} className="text-xs text-purple-400 hover:text-purple-300">+ Add</button>
                      )}
                    </div>
                  </div>
                );
              }
              return (
                <div key={subKey}>
                  <label className="text-xs text-gray-500">{typeof sub === 'object' ? sub.label : subKey}</label>
                  <AutoTextarea
                    value={item?.[subKey] || ''}
                    onChange={(e) => {
                      const nextItems = [...items];
                      nextItems[i] = { ...nextItems[i], [subKey]: e.target.value };
                      onChange(fieldKey, nextItems);
                    }}
                    disabled={disabled}
                    minRows={1}
                  />
                </div>
              );
            })}
            {!disabled && (
              <button
                onClick={() => onChange(fieldKey, items.filter((_, idx) => idx !== i))}
                className="text-xs text-red-400 hover:text-red-300"
              >
                Remove item
              </button>
            )}
          </div>
        ))}
        {!disabled && (
          <button
            onClick={() => {
              const empty = {};
              Object.keys(schema).forEach((k) => {
                const s = schema[k];
                const st = typeof s === 'object' ? s.type : s;
                empty[k] = st === 'list' ? [] : '';
              });
              onChange(fieldKey, [...items, empty]);
            }}
            className="w-full py-2 border border-dashed border-gray-600 rounded text-sm text-purple-400 hover:text-purple-300 hover:border-purple-500 transition-colors"
          >
            + Add {fieldSchema.label?.replace(/s$/i, '') || 'item'}
          </button>
        )}
      </div>
    );
  }

  return (
    <input
      type="text"
      value={value || ''}
      onChange={(e) => onChange(fieldKey, e.target.value)}
      disabled={disabled}
      className="w-full bg-gray-800 border border-gray-600 rounded px-3 py-1.5 text-gray-200 focus:border-purple-500 focus:outline-none break-words"
    />
  );
}

/**
 * SchemaForm — renders a step's editable fields purely from its schema.
 * This is the default step body; any step that only needs form fields uses it.
 */
export default function SchemaForm({ step, editedData, onFieldChange, disabled, onRerollItem, rerollingKey }) {
  const schema = step.schema || {};
  return (
    <div className="space-y-4">
      {Object.keys(schema).map((fieldKey) => {
        const fieldSchema = schema[fieldKey];
        return (
          <div key={fieldKey}>
            <label className="block text-sm font-medium text-gray-400 mb-1">
              {fieldSchema.label || fieldKey}
            </label>
            <StepField
              fieldKey={fieldKey}
              fieldSchema={fieldSchema}
              value={editedData?.[fieldKey]}
              onChange={onFieldChange}
              disabled={disabled}
              onRerollItem={onRerollItem}
              rerollingKey={rerollingKey}
            />
          </div>
        );
      })}
    </div>
  );
}
