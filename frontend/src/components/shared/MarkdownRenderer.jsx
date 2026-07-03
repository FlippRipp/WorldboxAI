import { useMemo, useCallback } from 'react';
import { renderMarkdown } from '../../lib/markdown';

export default function MarkdownRenderer({ content, streaming = false }) {
  const html = useMemo(() => renderMarkdown(content), [content]);

  // Copy buttons are rendered inside the sanitized HTML, so handle their
  // clicks by delegation instead of wiring listeners into the DOM per render.
  const onClick = useCallback((e) => {
    const btn = e.target.closest('.code-copy');
    if (!btn) return;
    const code = btn.closest('.code-block')?.querySelector('code');
    if (!code) return;
    navigator.clipboard.writeText(code.innerText).then(() => {
      btn.textContent = 'Copied';
      btn.classList.add('copied');
      setTimeout(() => {
        btn.textContent = 'Copy';
        btn.classList.remove('copied');
      }, 1500);
    });
  }, []);

  return (
    <div
      className={`md-content max-w-none${streaming ? ' streaming-md' : ''}`}
      onClick={onClick}
      dangerouslySetInnerHTML={{ __html: html }}
    />
  );
}
