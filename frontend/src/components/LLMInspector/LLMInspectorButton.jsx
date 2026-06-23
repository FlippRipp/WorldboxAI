import { useState, useRef, useEffect, useCallback } from 'react';
import { createPortal } from 'react-dom';
import { useLLMInspector } from '../../hooks/useLLMInspector';

const STORAGE_KEY = 'worldbox_llm_inspector_pos';
const DRAG_THRESHOLD = 4;

function loadPosition() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (raw) {
      const p = JSON.parse(raw);
      if (typeof p.x === 'number' && typeof p.y === 'number') return p;
    }
  } catch (_) {}
  return { x: 16, y: 16 };
}

function savePosition(pos) {
  try { localStorage.setItem(STORAGE_KEY, JSON.stringify(pos)); } catch (_) {}
}

export default function LLMInspectorButton() {
  const { calls, isOpen, togglePanel } = useLLMInspector();
  const [position, setPosition] = useState(loadPosition);
  const [dragging, setDragging] = useState(false);
  const dragRef = useRef(null);
  const buttonRef = useRef(null);

  const seen = new Set();
  for (const c of calls) seen.add(c.id);
  const count = seen.size;

  useEffect(() => {
    if (!dragging) return;
    const onMove = (e) => {
      if (!dragRef.current) return;
      const dx = dragRef.current.startX - e.clientX;
      const dy = dragRef.current.startY - e.clientY;
      const nx = Math.max(8, Math.min(400, dragRef.current.posX + dx));
      const ny = Math.max(8, Math.min(window.innerHeight - 60, dragRef.current.posY + dy));
      setPosition({ x: nx, y: ny });
    };
    const onUp = () => {
      setDragging(false);
      setPosition(p => { savePosition(p); return p; });
      dragRef.current = null;
    };
    window.addEventListener('mousemove', onMove);
    window.addEventListener('mouseup', onUp);
    return () => {
      window.removeEventListener('mousemove', onMove);
      window.removeEventListener('mouseup', onUp);
    };
  }, [dragging]);

  const onMouseDown = useCallback((e) => {
    if (e.button !== 0) return;
    dragRef.current = {
      startX: e.clientX,
      startY: e.clientY,
      posX: position.x,
      posY: position.y,
      moved: false,
    };
    setDragging('pending');
    e.preventDefault();
  }, [position]);

  const onMouseUp = useCallback((e) => {
    if (dragRef.current && !dragRef.current.moved) {
      togglePanel();
    }
    setDragging(false);
    dragRef.current = null;
  }, [togglePanel]);

  const onMouseMove = useCallback((e) => {
    if (!dragRef.current) return;
    const dx = Math.abs(dragRef.current.startX - e.clientX);
    const dy = Math.abs(dragRef.current.startY - e.clientY);
    if (dx > DRAG_THRESHOLD || dy > DRAG_THRESHOLD) {
      dragRef.current.moved = true;
      if (dragging === 'pending') setDragging(true);
    }
  }, [dragging]);

  const btn = (
    <button
      ref={buttonRef}
      onMouseDown={onMouseDown}
      onMouseUp={onMouseUp}
      onMouseMove={onMouseMove}
      className={`
        fixed z-[9999] select-none
        flex items-center gap-1.5 px-2.5 py-1.5 rounded-full
        text-xs font-medium shadow-lg
        ${dragging === true ? 'scale-110 cursor-grabbing' : 'cursor-grab'}
        ${isOpen
          ? 'bg-purple-700 text-white ring-2 ring-purple-500/50'
          : 'bg-gray-900/90 text-gray-300 border border-gray-700 hover:bg-gray-800 hover:border-purple-600/50'
        }
      `}
      style={{
        right: position.x,
        bottom: position.y,
      }}
      title="LLM Call Inspector"
    >
      <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2}
          d="M8 9l3 3-3 3m5 0h3M5 20h14a2 2 0 002-2V6a2 2 0 00-2-2H5a2 2 0 00-2 2v12a2 2 0 002 2z" />
      </svg>
      <span>{count}</span>
    </button>
  );

  return createPortal(btn, document.body);
}
