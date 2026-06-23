import React from 'react';

export default class WidgetErrorBoundary extends React.Component {
  constructor(props) {
    super(props);
    this.state = { hasError: false, error: null };
  }

  static getDerivedStateFromError(error) {
    return { hasError: true, error };
  }

  componentDidCatch(error, info) {
    console.error(`Widget "${this.props.modId}" crashed:`, error, info);
  }

  render() {
    if (this.state.hasError) {
      return (
        <div className="p-3 m-2 bg-red-900/40 border border-red-700/50 rounded-lg">
          <div className="flex items-center gap-2 text-red-300 text-sm">
            <svg className="w-4 h-4" fill="currentColor" viewBox="0 0 20 20">
              <path fillRule="evenodd" d="M18 10a8 8 0 11-16 0 8 8 0 0116 0zm-7 4a1 1 0 11-2 0 1 1 0 012 0zm-1-9a1 1 0 00-1 1v4a1 1 0 102 0V6a1 1 0 00-1-1z" />
            </svg>
            <span className="font-medium">Widget Error</span>
          </div>
          <p className="text-red-400 text-xs mt-1">{this.props.modId}: {this.state.error?.message || 'Unknown error'}</p>
        </div>
      );
    }
    return this.props.children;
  }
}
