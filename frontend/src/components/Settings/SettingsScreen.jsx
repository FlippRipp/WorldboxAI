import { useEffect, useRef, useState } from 'react';
import ModelSettings from '../ModelSettings/ModelSettings';
import AppearanceSettings from './AppearanceSettings';
import CheatSettings from './CheatSettings';
import LogSettings from './LogSettings';

const CATEGORIES = [
  { id: 'model', label: 'Model', icon: '⚙️', description: 'AI providers, models, and API keys' },
  { id: 'appearance', label: 'Appearance', icon: '🎨', description: 'Theme colors and presets' },
  { id: 'cheats', label: 'Cheats', icon: '🎲', description: 'Bend the rules — applies to every story' },
  { id: 'logs', label: 'Logs', icon: '📜', description: 'Download the LLM call log and dump save states' },
];

export default function SettingsScreen({ onBack }) {
  const [active, setActive] = useState(CATEGORIES[0].id);
  const scrollRef = useRef(null);
  const sectionRefs = useRef({});
  const clickScrolling = useRef(false);

  const scrollTo = (id) => {
    setActive(id);
    const el = sectionRefs.current[id];
    if (!el) return;
    // Suppress the scroll-spy briefly so it doesn't fight the smooth scroll.
    clickScrolling.current = true;
    el.scrollIntoView({ behavior: 'smooth', block: 'start' });
    setTimeout(() => { clickScrolling.current = false; }, 600);
  };

  // Scroll-spy: highlight the category whose section is nearest the top.
  useEffect(() => {
    const root = scrollRef.current;
    if (!root) return;
    const observer = new IntersectionObserver(
      (entries) => {
        if (clickScrolling.current) return;
        const visible = entries
          .filter((e) => e.isIntersecting)
          .sort((a, b) => a.boundingClientRect.top - b.boundingClientRect.top);
        if (visible[0]) setActive(visible[0].target.dataset.section);
      },
      { root, rootMargin: '-10% 0px -70% 0px', threshold: 0 }
    );
    for (const cat of CATEGORIES) {
      const el = sectionRefs.current[cat.id];
      if (el) observer.observe(el);
    }
    return () => observer.disconnect();
  }, []);

  return (
    <div className="h-dvh bg-gradient-to-br from-gray-950 via-gray-900 to-gray-950 flex flex-col">
      {/* Header */}
      <div className="shrink-0 px-4 sm:px-6 pt-6 pb-4 max-w-5xl mx-auto w-full">
        <button onClick={onBack} className="flex items-center gap-2 text-gray-400 hover:text-gray-200 transition-colors mb-4">
          <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15 19l-7-7 7-7" />
          </svg>
          Back to Menu
        </button>
        <h2 className="text-2xl font-bold text-gray-100">Settings</h2>
      </div>

      {/* Body: category rail + scrollable sections. The rail sits left on
          desktop and collapses to a horizontal chip row above the content on
          narrow screens. */}
      <div className="flex-1 min-h-0 max-w-5xl mx-auto w-full flex flex-col sm:flex-row gap-3 sm:gap-6 px-4 sm:px-6 pb-6">
        {/* Category rail */}
        <nav className="shrink-0 flex sm:flex-col sm:w-48 gap-2 sm:gap-1 overflow-x-auto">
          {CATEGORIES.map((cat) => (
            <button
              key={cat.id}
              onClick={() => scrollTo(cat.id)}
              className={`shrink-0 sm:w-full flex items-center gap-2 px-3 py-2 rounded-lg text-left text-sm whitespace-nowrap transition-colors ${
                active === cat.id
                  ? 'bg-purple-600/20 border border-purple-500 text-gray-100'
                  : 'border border-transparent text-gray-400 hover:bg-gray-800 hover:text-gray-200'
              }`}
            >
              <span>{cat.icon}</span>
              {cat.label}
            </button>
          ))}
        </nav>

        {/* Scrollable content */}
        <div ref={scrollRef} className="flex-1 min-w-0 overflow-y-auto book-scroll pr-2 space-y-10">
          {CATEGORIES.map((cat) => (
            <section
              key={cat.id}
              data-section={cat.id}
              ref={(el) => { sectionRefs.current[cat.id] = el; }}
              className="scroll-mt-4"
            >
              <div className="mb-4">
                <h3 className="text-lg font-semibold text-gray-100">{cat.label}</h3>
                <p className="text-xs text-gray-500">{cat.description}</p>
              </div>
              {cat.id === 'model' && <ModelSettings embedded />}
              {cat.id === 'appearance' && <AppearanceSettings />}
              {cat.id === 'cheats' && <CheatSettings />}
              {cat.id === 'logs' && <LogSettings />}
            </section>
          ))}
          {/* Tail spacer so the last section can scroll to the top */}
          <div className="h-[40vh]" />
        </div>
      </div>
    </div>
  );
}
