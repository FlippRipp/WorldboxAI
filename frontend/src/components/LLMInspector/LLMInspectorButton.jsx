import { useState, useRef, useEffect, useCallback } from 'react';
import { createPortal } from 'react-dom';
import { useLLMInspector } from '../../hooks/useLLMInspector';
import { useMediaQuery } from '../../hooks/useMediaQuery';
import { storage } from '../../lib/storage';

const STORAGE_KEY = 'worldbox_llm_inspector_pos';
const DRAG_THRESHOLD = 4;

function maxOffsetX() {
  return Math.max(8, window.innerWidth - 72);
}

function loadPosition() {
  try {
    const raw = storage.getItem(STORAGE_KEY);
    if (raw) {
      const p = JSON.parse(raw);
      if (typeof p.x === 'number' && typeof p.y === 'number') {
        return {
          x: Math.max(8, Math.min(maxOffsetX(), p.x)),
          y: Math.max(8, Math.min(window.innerHeight - 60, p.y)),
        };
      }
    }
  } catch (_) {}
  return { x: 16, y: 16 };
}

function savePosition(pos) {
  try { storage.setItem(STORAGE_KEY, JSON.stringify(pos)); } catch (_) {}
}

export default function LLMInspectorButton() {
  // On mobile (< 1024px), the trigger lives inside the Sidebar drawer so this
  // floating pill never occludes primary bottom-right Save/Send buttons.
  const isMobile = useMediaQuery('(max-width: 1023px)');
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
      if (Math.abs(dx) > DRAG_THRESHOLD || Math.abs(dy) > DRAG_THRESHOLD) {
        dragRef.current.moved = true;
        if (dragging === 'pending') setDragging(true);
      }
      if (!dragRef.current.moved) return;
      const nx = Math.max(8, Math.min(Math.min(400, maxOffsetX()), dragRef.current.posX + dx));
      const ny = Math.max(8, Math.min(window.innerHeight - 60, dragRef.current.posY + dy));
      setPosition({ x: nx, y: ny });
    };
    const onUp = () => {
      const wasTap = dragRef.current && !dragRef.current.moved;
      setDragging(false);
      if (!wasTap) {
        setPosition(p => { savePosition(p); return p; });
      }
      dragRef.current = null;
      if (wasTap) togglePanel();
    };
    window.addEventListener('pointermove', onMove);
    window.addEventListener('pointerup', onUp);
    window.addEventListener('pointercancel', onUp);
    return () => {
      window.removeEventListener('pointermove', onMove);
      window.removeEventListener('pointerup', onUp);
      window.removeEventListener('pointercancel', onUp);
    };
  }, [dragging, togglePanel]);

  const onPointerDown = useCallback((e) => {
    if (e.pointerType === 'mouse' && e.button !== 0) return;
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

  if (isMobile) return null;

  const btn = (
    <button
      ref={buttonRef}
      data-llm-inspector-btn="true"
      onPointerDown={onPointerDown}
      className={`
        fixed z-[9999] select-none touch-none
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
