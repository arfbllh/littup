'use client';

import React from 'react';
import type { LucideIcon } from 'lucide-react';

interface InputProps extends React.InputHTMLAttributes<HTMLInputElement> {
  icon?: LucideIcon;
  label?: string;
}

export function Input({ icon: Icon, label, style, ...props }: InputProps) {
  const [focused, setFocused] = React.useState(false);

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '4px' }}>
      {label && (
        <label
          style={{
            fontSize: '12px',
            fontWeight: 500,
            color: 'var(--paper-500)',
            fontFamily: 'var(--font-sans)',
          }}
        >
          {label}
        </label>
      )}
      <div style={{ position: 'relative' }}>
        {Icon && (
          <span
            style={{
              position: 'absolute',
              left: '10px',
              top: '50%',
              transform: 'translateY(-50%)',
              color: 'var(--paper-400)',
              display: 'flex',
              alignItems: 'center',
              pointerEvents: 'none',
            }}
          >
            <Icon size={14} />
          </span>
        )}
        <input
          {...props}
          onFocus={(e) => { setFocused(true); props.onFocus?.(e); }}
          onBlur={(e) => { setFocused(false); props.onBlur?.(e); }}
          style={{
            width: '100%',
            height: '32px',
            padding: Icon ? '0 10px 0 32px' : '0 10px',
            fontSize: '13px',
            fontFamily: 'var(--font-sans)',
            color: 'var(--paper-700)',
            backgroundColor: '#fff',
            border: `1px solid ${focused ? 'var(--ochre-500)' : 'var(--paper-300)'}`,
            borderRadius: 'var(--radius-input)',
            boxShadow: focused ? '0 0 0 2px var(--ochre-100)' : 'none',
            outline: 'none',
            transition: 'border-color 120ms, box-shadow 120ms',
            ...style,
          }}
        />
      </div>
    </div>
  );
}
