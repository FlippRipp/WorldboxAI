import WidgetErrorBoundary from '../shared/WidgetErrorBoundary';
import DynamicWidget from '../shared/DynamicWidget';

export default function SlotRenderer({ slotName, modules, state, config, eventBus, slotProps, skeleton = true, className }) {
  const slotModules = (modules || []).filter(mod => mod.ui_slots?.includes(slotName));

  if (slotModules.length === 0) return null;

  return (
    <>
      {slotModules.map(mod => (
        <WidgetErrorBoundary key={mod.id} modId={mod.id}>
          <DynamicWidget
            modId={mod.id}
            state={state}
            config={config?.[mod.id] || {}}
            slotName={slotName}
            eventBus={eventBus}
            slotProps={slotProps}
            skeleton={skeleton}
          />
        </WidgetErrorBoundary>
      ))}
    </>
  );
}
