'use client';

import React from 'react';

interface CardHeaderProps {
  eyebrow?: string;
  title?: string;
  action?: React.ReactNode;
  className?: string;
  style?: React.CSSProperties;
}

export function CardHeader({ eyebrow, title, action, className, style }: CardHeaderProps) {
  return (
    <div
      className={className}
      style={{
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'space-between',
        padding: '14px 16px',
        borderBottom: '1px solid var(--paper-300)',
        ...style,
      }}
    >
      <div>
        {eyebrow && (
          <div className="eyebrow" style={{ marginBottom: title ? '2px' : 0 }}>
            {eyebrow}
          </div>
        )}
        {title && (
          <div style={{ fontSize: '14px', fontWeight: 600, color: 'var(--paper-700)' }}>
            {title}
          </div>
        )}
      </div>
      {action && <div>{action}</div>}
    </div>
  );
}

interface CardProps {
  children: React.ReactNode;
  padding?: number | string;
  style?: React.CSSProperties;
  className?: string;
}

export function Card({ children, padding, style, className }: CardProps) {
  return (
    <div
      className={className}
      style={{
        backgroundColor: '#fff',
        border: '1px solid var(--paper-300)',
        borderRadius: 'var(--radius-card)',
        overflow: 'hidden',
        ...style,
      }}
    >
      {padding !== undefined ? (
        <div style={{ padding }}>{children}</div>
      ) : (
        children
      )}
    </div>
  );
}

interface CardBodyProps {
  children: React.ReactNode;
  padding?: string | number;
  style?: React.CSSProperties;
}

export function CardBody({ children, padding = '16px', style }: CardBodyProps) {
  return (
    <div style={{ padding, ...style }}>
      {children}
    </div>
  );
}
