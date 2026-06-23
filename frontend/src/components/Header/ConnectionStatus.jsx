export default function ConnectionStatus({ isConnected, isReconnecting }) {
  if (isConnected) return null;

  return (
    <div className={`px-4 py-1.5 text-center text-sm ${
      isReconnecting
        ? 'bg-yellow-900/60 text-yellow-200'
        : 'bg-red-900/60 text-red-200'
    }`} role="alert">
      {isReconnecting
        ? <>Reconnecting to server... <span className="inline-block ml-1 animate-spin">&#8635;</span></>
        : 'Disconnected from server'}
    </div>
  );
}
