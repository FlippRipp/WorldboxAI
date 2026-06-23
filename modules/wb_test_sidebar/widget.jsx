import React from 'react';

export default function TestSidebarWidget({ state, config, slotName }) {
  return (
    <div style={{
      background: 'linear-gradient(135deg, #667eea 0%, #764ba2 100%)',
      color: 'white',
      padding: '16px',
      borderRadius: '8px',
      marginBottom: '8px',
      fontFamily: 'monospace',
      boxShadow: '0 4px 15px rgba(102, 126, 234, 0.4)',
    }}>
      <div style={{ fontSize: '18px', fontWeight: 'bold', marginBottom: '4px' }}>
        TEST SIDEBAR WORKS
      </div>
      <div style={{ fontSize: '12px', opacity: 0.85 }}>
        module_data: {JSON.stringify(state?.module_data ? Object.keys(state.module_data) : 'NONE')}
      </div>
    </div>
  );
}
