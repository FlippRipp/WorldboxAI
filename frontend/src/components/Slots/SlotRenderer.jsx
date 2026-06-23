import WidgetErrorBoundary from '../shared/WidgetErrorBoundary';
import DynamicWidget from '../shared/DynamicWidget';

export default function SlotRenderer({ slotName, modules, state, config, eventBus, className }) {
  const slotModules = modules.filter(mod => {
    const match = mod.ui_slots?.includes(slotName);
    if (slotName === 'slot_sidebar') console.log(`[SlotRenderer] module=${mod.id} ui_slots=${JSON.stringify(mod.ui_slots)} match=${match}`);
    return match;
  });

  console.log(`[SlotRenderer] slot=${slotName} totalModules=${modules.length} matchedModules=${slotModules.length}`);

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
          />
        </WidgetErrorBoundary>
      ))}
    </>
  );
}
