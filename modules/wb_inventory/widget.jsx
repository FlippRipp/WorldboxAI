import React, { useState } from 'react';

export default function InventoryWidget({ state, config }) {
  const [showItems, setShowItems] = useState(true);

  const inv = state?.module_data?.wb_inventory;
  if (!inv) return null;

  const items = inv.items ?? [];
  const currency = inv.currency ?? 0;
  const currencyName = config?.currency_name || 'gold';
  const totalCarried = items.reduce((sum, it) => sum + (it.qty ?? 1), 0);

  return (
    <div className="bg-gray-900/70 rounded-lg border border-gray-700 p-3 space-y-3 text-sm">
      <div className="flex items-center justify-between">
        <span className="text-gray-300 font-semibold">Inventory</span>
        <span className="flex items-center gap-1 text-amber-400 font-mono text-xs">
          <span className="text-amber-500">{'●'}</span>
          {currency} <span className="text-amber-400/70 capitalize">{currencyName}</span>
        </span>
      </div>

      <button
        onClick={() => setShowItems(!showItems)}
        className="w-full flex items-center justify-between text-xs text-gray-400 hover:text-gray-200 transition-colors"
      >
        <span className="uppercase tracking-wider">Items ({totalCarried})</span>
        <span className="text-gray-500">{showItems ? '▼' : '▶'}</span>
      </button>

      {showItems && (
        items.length > 0 ? (
          <div className="space-y-1.5">
            {items.map((item, i) => (
              <div key={`${item.name}-${i}`}>
                <div className="flex items-center justify-between text-xs">
                  <span className="text-gray-300 truncate">{item.name}</span>
                  <span className="text-gray-500 font-mono ml-2 shrink-0">x{item.qty ?? 1}</span>
                </div>
                {item.description && (
                  <div className="text-[10px] text-gray-600 leading-tight">{item.description}</div>
                )}
              </div>
            ))}
          </div>
        ) : (
          <div className="text-xs text-gray-600 italic">Nothing carried.</div>
        )
      )}
    </div>
  );
}
