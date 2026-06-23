export default function SkeletonLoader({ height = 'h-16', width = 'w-full', className = '' }) {
  return (
    <div className={`${height} ${width} ${className} animate-pulse rounded-lg bg-gray-800/50`}>
      <div className="flex items-center gap-3 p-3">
        <div className="w-8 h-8 rounded-full bg-gray-700" />
        <div className="flex-1 space-y-2">
          <div className="h-3 bg-gray-700 rounded w-3/4" />
          <div className="h-3 bg-gray-700 rounded w-1/2" />
        </div>
      </div>
    </div>
  );
}
