import React from 'react';

interface PageHeaderProps {
  eyebrow?: string;
  title: string;
  actions?: React.ReactNode;
  style?: React.CSSProperties;
}

export function PageHeader({ eyebrow, title, actions, style }: PageHeaderProps) {
  return (
    <div
      style={{
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'space-between',
        padding: '20px 24px 16px',
        borderBottom: '1px solid var(--paper-300)',
        backgroundColor: '#fff',
        flexShrink: 0,
        ...style,
      }}
    >
      <div>
        {eyebrow && (
          <div className="eyebrow" style={{ marginBottom: '4px' }}>
            {eyebrow}
          </div>
        )}
        <h2
          style={{
            margin: 0,
            fontSize: '18px',
            fontWeight: 700,
            color: 'var(--paper-800)',
            fontFamily: 'var(--font-sans)',
            lineHeight: 1.2,
          }}
        >
          {title}
        </h2>
      </div>
      {actions && (
        <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
          {actions}
        </div>
      )}
    </div>
  );
}
