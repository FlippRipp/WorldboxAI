import { useCallback, useEffect, useMemo, useState } from 'react';
import { api } from '../../lib/api';

const RANGES = [
  { id: '24h', label: '24h' },
  { id: '7d', label: '7d' },
  { id: '30d', label: '30d' },
  { id: '90d', label: '90d' },
  { id: 'all', label: 'All Time' },
];

const CALL_TYPE_NAMES = {
  storyteller: 'Storyteller',
  reader: 'Reader',
  embedding: 'Embeddings',
  librarian: 'Librarian',
  world_build: 'World Build',
  world_ideation: 'World Ideation',
  character_build: 'Character Build',
  module_fast: 'Module LLM',
  search: 'Web Search',
  decision: 'Jev Decisions',
  image_gen: 'Cloud Images',
  diagnostic: 'Diagnostic',
};

function formatUsd(val, { compact = false } = {}) {
  const n = Number(val || 0);
  if (!Number.isFinite(n) || n === 0) return '$0.00';
  if (n >= 100) return `$${n.toFixed(2)}`;
  if (n >= 1) return `$${n.toFixed(compact ? 2 : 3)}`;
  if (n >= 0.01) return `$${n.toFixed(compact ? 3 : 4)}`;
  return `$${n.toFixed(compact ? 4 : 6)}`;
}

function formatTokens(n) {
  const v = Number(n || 0);
  if (v >= 1_000_000) return `${(v / 1_000_000).toFixed(2)}M`;
  if (v >= 1_000) return `${(v / 1_000).toFixed(1)}K`;
  return String(v);
}

function CostChart({ buckets, showBase, showAccumulated, granularity }) {
  const [hoverIdx, setHoverIdx] = useState(null);

  const width = 880;
  const height = 280;
  const padLeft = 62;
  const padRight = showBase && showAccumulated ? 62 : 24;
  const padTop = 20;
  const padBottom = 36;
  const innerW = width - padLeft - padRight;
  const innerH = height - padTop - padBottom;

  const maxBase = useMemo(() => {
    if (!buckets.length) return 0;
    return Math.max(...buckets.map((b) => b.base_cost_usd || 0), 0);
  }, [buckets]);

  const maxAcc = useMemo(() => {
    if (!buckets.length) return 0;
    return Math.max(...buckets.map((b) => b.accumulated_cost_usd || 0), 0);
  }, [buckets]);

  const baseCeil = maxBase > 0 ? maxBase * 1.12 : 0.01;
  const accCeil = maxAcc > 0 ? maxAcc * 1.08 : 0.01;

  if (!buckets.length) {
    return (
      <div className="h-64 flex items-center justify-center text-sm text-gray-500">
        No API usage recorded for the selected filters yet.
      </div>
    );
  }

  const count = buckets.length;
  const slotW = innerW / count;
  const barW = Math.max(3, Math.min(28, slotW * 0.68));

  const xFor = (i) => padLeft + slotW * i + slotW / 2;
  const yBase = (v) => padTop + innerH - (Math.min(v, baseCeil) / baseCeil) * innerH;
  const yAcc = (v) => {
    const ceil = showBase ? accCeil : accCeil;
    return padTop + innerH - (Math.min(v, ceil) / ceil) * innerH;
  };

  const linePoints = buckets
    .map((b, i) => `${xFor(i).toFixed(1)},${yAcc(b.accumulated_cost_usd || 0).toFixed(1)}`)
    .join(' ');

  const areaPath = buckets.length > 0
    ? `M ${xFor(0).toFixed(1)},${(padTop + innerH).toFixed(1)} L `
      + buckets.map((b, i) => `${xFor(i).toFixed(1)},${yAcc(b.accumulated_cost_usd || 0).toFixed(1)}`).join(' L ')
      + ` L ${xFor(buckets.length - 1).toFixed(1)},${(padTop + innerH).toFixed(1)} Z`
    : '';

  const tickSteps = [0, 0.25, 0.5, 0.75, 1];
  const labelStride = Math.max(1, Math.ceil(count / 8));
  const hovered = hoverIdx != null && buckets[hoverIdx] ? buckets[hoverIdx] : null;

  return (
    <div className="relative select-none">
      {/* Tooltip banner */}
      <div className="min-h-7 mb-2 flex flex-wrap items-center justify-between gap-2 text-xs">
        {hovered ? (
          <div className="flex flex-wrap items-center gap-x-4 gap-y-1 bg-gray-800/90 border border-gray-700 rounded-lg px-3 py-1">
            <span className="font-medium text-gray-200">{hovered.tooltip_label}</span>
            {showBase && (
              <span className="text-sky-300">
                Base ({granularity}): <span className="font-mono font-semibold">{formatUsd(hovered.base_cost_usd)}</span>
              </span>
            )}
            {showAccumulated && (
              <span className="text-emerald-300">
                Accumulated: <span className="font-mono font-semibold">{formatUsd(hovered.accumulated_cost_usd)}</span>
              </span>
            )}
            <span className="text-gray-400">
              {hovered.calls} {hovered.calls === 1 ? 'call' : 'calls'}
              {(hovered.tokens_in > 0 || hovered.tokens_out > 0) && (
                <span className="ml-1 text-gray-500">
                  ({formatTokens(hovered.tokens_in)} in / {formatTokens(hovered.tokens_out)} out)
                </span>
              )}
            </span>
          </div>
        ) : (
          <span className="text-gray-500">
            Hover or tap a {granularity} bucket to inspect exact base and accumulated spend.
          </span>
        )}
      </div>

      <svg
        viewBox={`0 0 ${width} ${height}`}
        className="w-full h-auto overflow-visible"
        onMouseLeave={() => setHoverIdx(null)}
        role="img"
        aria-label="API cost over time chart"
      >
        <defs>
          <linearGradient id="wbAccArea" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="#34d399" stopOpacity="0.22" />
            <stop offset="100%" stopColor="#34d399" stopOpacity="0.0" />
          </linearGradient>
        </defs>

        {/* Horizontal grid lines + Y axis ticks */}
        {tickSteps.map((t) => {
          const y = padTop + innerH - t * innerH;
          const leftVal = (showBase ? baseCeil : accCeil) * t;
          const rightVal = accCeil * t;
          return (
            <g key={t}>
              <line
                x1={padLeft}
                x2={width - padRight}
                y1={y}
                y2={y}
                stroke="#1f2937"
                strokeWidth="1"
              />
              <text
                x={padLeft - 8}
                y={y + 3}
                textAnchor="end"
                className={`text-[10px] font-mono ${showBase ? 'fill-sky-400/80' : 'fill-emerald-400/80'}`}
              >
                {formatUsd(leftVal, { compact: true })}
              </text>
              {showBase && showAccumulated && (
                <text
                  x={width - padRight + 8}
                  y={y + 3}
                  textAnchor="start"
                  className="text-[10px] font-mono fill-emerald-400/80"
                >
                  {formatUsd(rightVal, { compact: true })}
                </text>
              )}
            </g>
          );
        })}

        {/* Hover column highlight */}
        {hoverIdx != null && buckets[hoverIdx] && (
          <rect
            x={padLeft + slotW * hoverIdx}
            y={padTop}
            width={slotW}
            height={innerH}
            fill="rgba(255,255,255,0.04)"
          />
        )}

        {/* Base spend bars */}
        {showBase && buckets.map((b, i) => {
          const val = b.base_cost_usd || 0;
          const y = yBase(val);
          const barH = Math.max(val > 0 ? 2 : 0, padTop + innerH - y);
          const isHover = hoverIdx === i;
          return (
            <rect
              key={b.bucket_start}
              x={xFor(i) - barW / 2}
              y={padTop + innerH - barH}
              width={barW}
              height={barH}
              rx="2"
              className={isHover ? 'fill-sky-400' : 'fill-sky-500/75'}
            />
          );
        })}

        {/* Accumulated area + curve */}
        {showAccumulated && (
          <>
            <path d={areaPath} fill="url(#wbAccArea)" />
            <polyline
              fill="none"
              stroke="#34d399"
              strokeWidth="2.2"
              strokeLinejoin="round"
              strokeLinecap="round"
              points={linePoints}
            />
            {buckets.map((b, i) => {
              const isHover = hoverIdx === i;
              if (!isHover && count > 45) return null;
              return (
                <circle
                  key={b.bucket_start}
                  cx={xFor(i)}
                  cy={yAcc(b.accumulated_cost_usd || 0)}
                  r={isHover ? 4.5 : 2.5}
                  className={isHover ? 'fill-emerald-300 stroke-gray-950' : 'fill-emerald-400'}
                  strokeWidth={isHover ? 1.5 : 0}
                />
              );
            })}
          </>
        )}

        {/* X axis labels */}
        {buckets.map((b, i) => {
          if (i % labelStride !== 0 && i !== count - 1) return null;
          if (i === count - 1 && count > 4 && (count - 1) % labelStride < labelStride / 2) return null;
          return (
            <text
              key={b.bucket_start}
              x={xFor(i)}
              y={height - 10}
              textAnchor="middle"
              className="text-[10px] fill-gray-500 font-mono"
            >
              {b.label}
            </text>
          );
        })}

        {/* Invisible hit targets across full bucket slots */}
        {buckets.map((b, i) => (
          <rect
            key={`hit-${b.bucket_start}`}
            x={padLeft + slotW * i}
            y={padTop}
            width={slotW}
            height={innerH}
            fill="transparent"
            className="cursor-crosshair"
            onMouseEnter={() => setHoverIdx(i)}
            onClick={() => setHoverIdx(i)}
          />
        ))}
      </svg>
    </div>
  );
}

function MultiFilterPills({ label, options, selected, onToggle, onClear }) {
  if (!options.length) return null;
  return (
    <div className="space-y-1.5">
      <div className="flex items-center justify-between">
        <span className="text-[11px] font-semibold uppercase tracking-wider text-gray-500">{label}</span>
        {selected.length > 0 && (
          <button
            onClick={onClear}
            className="text-[11px] text-purple-400 hover:text-purple-300 transition-colors"
          >
            All ({selected.length} selected)
          </button>
        )}
      </div>
      <div className="flex flex-wrap gap-1.5">
        {options.map((opt) => {
          const active = selected.includes(opt.id);
          return (
            <button
              key={opt.id}
              onClick={() => onToggle(opt.id)}
              className={`px-2.5 py-1 rounded-lg text-xs font-medium border transition-colors truncate max-w-[20rem] ${
                active
                  ? 'bg-purple-600/30 border-purple-500 text-purple-200'
                  : 'bg-gray-800/70 border-gray-700/80 text-gray-400 hover:text-gray-200 hover:border-gray-600'
              }`}
              title={opt.title || opt.label}
            >
              {opt.label}
            </button>
          );
        })}
      </div>
    </div>
  );
}

export default function UsageScreen({ onBack }) {
  const [range, setRange] = useState('30d');
  const [selectedModels, setSelectedModels] = useState([]);
  const [selectedModules, setSelectedModules] = useState([]);
  const [showBase, setShowBase] = useState(true);
  const [showAccumulated, setShowAccumulated] = useState(true);
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [confirmReset, setConfirmReset] = useState(false);

  const loadSummary = useCallback(async () => {
    setError('');
    try {
      const res = await api.getUsageSummary({
        range,
        models: selectedModels,
        modules: selectedModules,
      });
      setData(res);
    } catch (e) {
      setError(e.message || 'Failed to load usage data.');
    } finally {
      setLoading(false);
    }
  }, [range, selectedModels, selectedModules]);

  useEffect(() => {
    let active = true;
    api.getUsageSummary({
      range,
      models: selectedModels,
      modules: selectedModules,
    })
      .then((res) => {
        if (!active) return;
        setData(res);
        setError('');
      })
      .catch((e) => {
        if (!active) return;
        setError(e.message || 'Failed to load usage data.');
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => { active = false; };
  }, [range, selectedModels, selectedModules]);

  const toggleModel = (modelId) => {
    setSelectedModels((prev) =>
      prev.includes(modelId) ? prev.filter((m) => m !== modelId) : [...prev, modelId]
    );
  };

  const toggleModule = (modId) => {
    setSelectedModules((prev) =>
      prev.includes(modId) ? prev.filter((m) => m !== modId) : [...prev, modId]
    );
  };

  const handleReset = async () => {
    try {
      await api.clearUsageHistory();
      setConfirmReset(false);
      setSelectedModels([]);
      setSelectedModules([]);
      await loadSummary();
    } catch (e) {
      setError(e.message || 'Failed to clear usage history.');
    }
  };

  const totals = data?.totals || {};
  const buckets = data?.buckets || [];
  const byModel = data?.by_model || [];
  const byModule = data?.by_module || [];
  const byCallType = data?.by_call_type || [];
  const modelOptions = (data?.available_models || []).map((m) => ({ id: m, label: m, title: m }));
  const moduleOptions = (data?.available_modules || []).map((m) => ({
    id: m.id,
    label: m.label,
    title: `${m.label} (${m.id})`,
  }));

  const hasFilters = selectedModels.length > 0 || selectedModules.length > 0;
  const maxModuleCost = Math.max(...byModule.map((r) => r.cost_usd || 0), 0.000001);
  const maxModelCost = Math.max(...byModel.map((r) => r.cost_usd || 0), 0.000001);

  return (
    <div className="min-h-screen bg-gradient-to-br from-gray-950 via-gray-900 to-gray-950 text-gray-100 p-4 sm:p-6">
      <div className="max-w-6xl mx-auto space-y-6 pb-12">
        {/* Top bar */}
        <div className="flex flex-wrap items-center justify-between gap-4">
          <div>
            <button
              onClick={onBack}
              className="flex items-center gap-2 text-gray-400 hover:text-gray-200 transition-colors mb-3 text-sm"
            >
              <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15 19l-7-7 7-7" />
              </svg>
              Back to Menu
            </button>
            <h1 className="text-2xl sm:text-3xl font-bold text-gray-100">API Usage &amp; Cost</h1>
            <p className="text-xs text-gray-500 mt-1">
              Tracks completions, embeddings, web search, Jev decisions, and cloud image generation.
            </p>
          </div>

          <div className="flex items-center gap-2">
            {/* Time range selector */}
            <div className="flex bg-gray-900 border border-gray-800 rounded-lg p-0.5">
              {RANGES.map((r) => (
                <button
                  key={r.id}
                  onClick={() => setRange(r.id)}
                  className={`px-2.5 py-1 rounded-md text-xs font-medium transition-colors ${
                    range === r.id
                      ? 'bg-purple-600 text-white'
                      : 'text-gray-400 hover:text-gray-200'
                  }`}
                >
                  {r.label}
                </button>
              ))}
            </div>

            <button
              onClick={loadSummary}
              className="px-3 py-1.5 rounded-lg bg-gray-800 hover:bg-gray-700 border border-gray-700 text-xs text-gray-300 transition-colors"
              title="Refresh usage stats"
            >
              Refresh
            </button>

            {!confirmReset ? (
              <button
                onClick={() => setConfirmReset(true)}
                className="px-3 py-1.5 rounded-lg bg-gray-900 hover:bg-red-950/50 border border-gray-800 hover:border-red-800/60 text-xs text-gray-400 hover:text-red-300 transition-colors"
              >
                Reset
              </button>
            ) : (
              <div className="flex items-center gap-1.5 bg-red-950/40 border border-red-800/60 rounded-lg px-2 py-1">
                <span className="text-xs text-red-200">Clear ledger?</span>
                <button
                  onClick={handleReset}
                  className="px-2 py-0.5 rounded bg-red-600 hover:bg-red-500 text-xs text-white font-medium"
                >
                  Yes
                </button>
                <button
                  onClick={() => setConfirmReset(false)}
                  className="px-2 py-0.5 rounded bg-gray-800 hover:bg-gray-700 text-xs text-gray-300"
                >
                  Cancel
                </button>
              </div>
            )}
          </div>
        </div>

        {error && (
          <div className="bg-red-950/50 border border-red-800/60 rounded-xl p-3 text-sm text-red-200">
            {error}
          </div>
        )}

        {/* Filter bar */}
        {(moduleOptions.length > 0 || modelOptions.length > 0) && (
          <div className="bg-gray-900/70 border border-gray-800 rounded-xl p-4 space-y-3">
            <MultiFilterPills
              label="Filter by Module"
              options={moduleOptions}
              selected={selectedModules}
              onToggle={toggleModule}
              onClear={() => setSelectedModules([])}
            />
            <MultiFilterPills
              label="Filter by Model"
              options={modelOptions}
              selected={selectedModels}
              onToggle={toggleModel}
              onClear={() => setSelectedModels([])}
            />
            {hasFilters && (
              <div className="pt-1 border-t border-gray-800/80 flex items-center justify-between text-xs text-gray-400">
                <span>
                  Showing filtered spend ({selectedModules.length || 'all'} modules, {selectedModels.length || 'all'} models)
                </span>
                <button
                  onClick={() => {
                    setSelectedModels([]);
                    setSelectedModules([]);
                  }}
                  className="text-purple-400 hover:text-purple-300 font-medium"
                >
                  Reset all filters
                </button>
              </div>
            )}
          </div>
        )}

        {/* Tally cards */}
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
          <div className="bg-gray-900/70 border border-gray-800 rounded-xl p-4">
            <div className="text-xs font-medium text-gray-400 uppercase tracking-wider">
              {range === 'all' ? 'Total Spend' : `Spend (${range})`}
            </div>
            <div className="text-2xl font-bold font-mono text-emerald-300 mt-1">
              {formatUsd(totals.cost_usd)}
            </div>
            <div className="text-xs text-gray-500 mt-1">
              {range !== 'all'
                ? `All-time${hasFilters ? ' (filtered)' : ''}: ${formatUsd(totals.all_time_cost_usd)}`
                : `${totals.priced_calls || 0} priced calls`}
            </div>
          </div>

          <div className="bg-gray-900/70 border border-gray-800 rounded-xl p-4">
            <div className="text-xs font-medium text-gray-400 uppercase tracking-wider">API Calls</div>
            <div className="text-2xl font-bold font-mono text-gray-100 mt-1">
              {(totals.calls || 0).toLocaleString()}
            </div>
            <div className="text-xs text-gray-500 mt-1">
              {totals.unpriced_calls > 0
                ? `${totals.unpriced_calls} without rate info`
                : totals.error_calls > 0
                  ? `${totals.error_calls} failed`
                  : 'All calls accounted for'}
            </div>
          </div>

          <div className="bg-gray-900/70 border border-gray-800 rounded-xl p-4">
            <div className="text-xs font-medium text-gray-400 uppercase tracking-wider">Tokens Processed</div>
            <div className="text-2xl font-bold font-mono text-sky-300 mt-1">
              {formatTokens((totals.tokens_in || 0) + (totals.tokens_out || 0))}
            </div>
            <div className="text-xs text-gray-500 mt-1 font-mono">
              {formatTokens(totals.tokens_in)} in · {formatTokens(totals.tokens_out)} out
            </div>
          </div>

          <div className="bg-gray-900/70 border border-gray-800 rounded-xl p-4">
            <div className="text-xs font-medium text-gray-400 uppercase tracking-wider">Avg Cost / Call</div>
            <div className="text-2xl font-bold font-mono text-purple-300 mt-1">
              {formatUsd(totals.avg_cost_usd)}
            </div>
            <div className="text-xs text-gray-500 mt-1">
              Across {totals.priced_calls || 0} billable calls
            </div>
          </div>
        </div>

        {/* Cost over time chart */}
        <div className="bg-gray-900/70 border border-gray-800 rounded-xl p-4 sm:p-5">
          <div className="flex flex-wrap items-center justify-between gap-3 mb-3">
            <div>
              <h2 className="text-base font-semibold text-gray-100">Cost Over Time</h2>
              <p className="text-xs text-gray-500">
                Base spend per {data?.granularity || 'day'} and accumulated total across the window
              </p>
            </div>
            <div className="flex items-center gap-2">
              <button
                onClick={() => {
                  if (showBase && !showAccumulated) return;
                  setShowBase((v) => !v);
                }}
                className={`flex items-center gap-1.5 px-2.5 py-1 rounded-lg text-xs border transition-colors ${
                  showBase
                    ? 'bg-sky-950/60 border-sky-600/60 text-sky-200'
                    : 'bg-gray-800/60 border-gray-700 text-gray-500'
                }`}
              >
                <span className="w-2.5 h-2.5 rounded-sm bg-sky-400 inline-block" />
                Base
              </button>
              <button
                onClick={() => {
                  if (showAccumulated && !showBase) return;
                  setShowAccumulated((v) => !v);
                }}
                className={`flex items-center gap-1.5 px-2.5 py-1 rounded-lg text-xs border transition-colors ${
                  showAccumulated
                    ? 'bg-emerald-950/60 border-emerald-600/60 text-emerald-200'
                    : 'bg-gray-800/60 border-gray-700 text-gray-500'
                }`}
              >
                <span className="w-2.5 h-0.5 bg-emerald-400 inline-block" />
                Accumulated
              </button>
            </div>
          </div>

          {loading && !data ? (
            <div className="h-64 flex items-center justify-center text-sm text-gray-500">
              Loading usage history…
            </div>
          ) : (
            <CostChart
              buckets={buckets}
              showBase={showBase}
              showAccumulated={showAccumulated}
              granularity={data?.granularity || 'day'}
            />
          )}
        </div>

        {/* Breakdown by Module & Model */}
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
          {/* By Module */}
          <div className="bg-gray-900/70 border border-gray-800 rounded-xl p-4 sm:p-5">
            <div className="flex items-center justify-between mb-3">
              <h2 className="text-sm font-semibold text-gray-200">Spend by Module</h2>
              <span className="text-[11px] text-gray-500">Click row to filter</span>
            </div>
            {byModule.length === 0 ? (
              <p className="text-xs text-gray-500 py-6 text-center">No module activity in this window.</p>
            ) : (
              <div className="space-y-2">
                {byModule.map((row) => {
                  const pct = Math.min(100, ((row.cost_usd || 0) / maxModuleCost) * 100);
                  const active = selectedModules.includes(row.module);
                  return (
                    <button
                      key={row.module}
                      onClick={() => toggleModule(row.module)}
                      className={`w-full text-left p-2.5 rounded-lg border transition-colors ${
                        active
                          ? 'bg-purple-950/40 border-purple-500/60'
                          : 'bg-gray-950/50 border-gray-800/80 hover:border-gray-700'
                      }`}
                    >
                      <div className="flex items-center justify-between gap-2 text-xs mb-1">
                        <span className="font-medium text-gray-200 truncate">
                          {row.label}
                          <span className="ml-1.5 text-[11px] text-gray-500 font-mono">{row.module}</span>
                        </span>
                        <span className="font-mono font-semibold text-emerald-300 shrink-0">
                          {formatUsd(row.cost_usd)}
                        </span>
                      </div>
                      <div className="w-full h-1.5 bg-gray-800 rounded-full overflow-hidden mb-1">
                        <div
                          className="h-full bg-purple-500/80 rounded-full"
                          style={{ width: `${Math.max(row.cost_usd > 0 ? 2 : 0, pct)}%` }}
                        />
                      </div>
                      <div className="flex items-center justify-between text-[11px] text-gray-500 font-mono">
                        <span>{row.calls} {row.calls === 1 ? 'call' : 'calls'}</span>
                        <span>{formatTokens(row.tokens_in)} in · {formatTokens(row.tokens_out)} out</span>
                      </div>
                    </button>
                  );
                })}
              </div>
            )}
          </div>

          {/* By Model */}
          <div className="bg-gray-900/70 border border-gray-800 rounded-xl p-4 sm:p-5">
            <div className="flex items-center justify-between mb-3">
              <h2 className="text-sm font-semibold text-gray-200">Spend by Model</h2>
              <span className="text-[11px] text-gray-500">Click row to filter</span>
            </div>
            {byModel.length === 0 ? (
              <p className="text-xs text-gray-500 py-6 text-center">No model activity in this window.</p>
            ) : (
              <div className="space-y-2">
                {byModel.map((row) => {
                  const pct = Math.min(100, ((row.cost_usd || 0) / maxModelCost) * 100);
                  const active = selectedModels.includes(row.model);
                  return (
                    <button
                      key={row.model}
                      onClick={() => toggleModel(row.model)}
                      className={`w-full text-left p-2.5 rounded-lg border transition-colors ${
                        active
                          ? 'bg-purple-950/40 border-purple-500/60'
                          : 'bg-gray-950/50 border-gray-800/80 hover:border-gray-700'
                      }`}
                    >
                      <div className="flex items-center justify-between gap-2 text-xs mb-1">
                        <span className="font-mono text-gray-200 truncate" title={row.model}>
                          {row.model}
                        </span>
                        <span className="font-mono font-semibold text-emerald-300 shrink-0">
                          {formatUsd(row.cost_usd)}
                        </span>
                      </div>
                      <div className="w-full h-1.5 bg-gray-800 rounded-full overflow-hidden mb-1">
                        <div
                          className="h-full bg-sky-500/80 rounded-full"
                          style={{ width: `${Math.max(row.cost_usd > 0 ? 2 : 0, pct)}%` }}
                        />
                      </div>
                      <div className="flex items-center justify-between text-[11px] text-gray-500 font-mono">
                        <span>
                          {row.calls} {row.calls === 1 ? 'call' : 'calls'}
                          {row.unpriced_calls > 0 && ` (${row.unpriced_calls} unpriced)`}
                        </span>
                        <span>{formatTokens(row.tokens_in)} in · {formatTokens(row.tokens_out)} out</span>
                      </div>
                    </button>
                  );
                })}
              </div>
            )}
          </div>
        </div>

        {/* By Call Type strip (highlights Jev, Search, Embeddings, Storyteller, Cloud Images, etc.) */}
        {byCallType.length > 0 && (
          <div className="bg-gray-900/70 border border-gray-800 rounded-xl p-4 sm:p-5">
            <h2 className="text-sm font-semibold text-gray-200 mb-3">Spend by Service / Call Type</h2>
            <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-5 gap-3">
              {byCallType.map((ct) => (
                <div
                  key={ct.call_type}
                  className="bg-gray-950/60 border border-gray-800/80 rounded-lg p-3"
                >
                  <div className="text-xs text-gray-400 truncate">
                    {CALL_TYPE_NAMES[ct.call_type] || ct.call_type}
                  </div>
                  <div className="text-sm font-mono font-semibold text-emerald-300 mt-1">
                    {formatUsd(ct.cost_usd)}
                  </div>
                  <div className="text-[11px] text-gray-500 font-mono mt-0.5">
                    {ct.calls} {ct.calls === 1 ? 'call' : 'calls'}
                  </div>
                </div>
              ))}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
