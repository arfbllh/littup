'use client';

import React from 'react';
import type { LucideIcon } from 'lucide-react';

type Variant = 'primary' | 'secondary' | 'ghost' | 'danger';
type Size = 'sm' | 'md' | 'lg';

interface ButtonProps extends React.ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: Variant;
  size?: Size;
  icon?: LucideIcon;
  iconPosition?: 'left' | 'right';
  loading?: boolean;
}

const variantStyles: Record<Variant, React.CSSProperties> = {
  primary: {
    backgroundColor: 'var(--ochre-500)',
    color: '#fff',
    border: '1px solid var(--ochre-600)',
  },
  secondary: {
    backgroundColor: '#fff',
    color: 'var(--paper-700)',
    border: '1px solid var(--paper-300)',
  },
  ghost: {
    backgroundColor: 'transparent',
    color: 'var(--paper-600)',
    border: '1px solid transparent',
  },
  danger: {
    backgroundColor: '#fff',
    color: 'var(--cite-unsupported)',
    border: '1px solid var(--cite-unsupported)',
  },
};

const sizeStyles: Record<Size, React.CSSProperties> = {
  sm: { fontSize: '12px', padding: '4px 10px', height: '28px' },
  md: { fontSize: '13px', padding: '6px 14px', height: '32px' },
  lg: { fontSize: '14px', padding: '8px 20px', height: '38px' },
};

const hoverStyles: Record<Variant, React.CSSProperties> = {
  primary: { backgroundColor: 'var(--ochre-600)' },
  secondary: { backgroundColor: 'var(--paper-100)' },
  ghost: { backgroundColor: 'var(--paper-100)' },
  danger: { backgroundColor: 'var(--cite-unsupported-bg)' },
};

export function Button({
  variant = 'secondary',
  size = 'md',
  icon: Icon,
  iconPosition = 'left',
  loading = false,
  children,
  disabled,
  style,
  ...props
}: ButtonProps) {
  const [hovered, setHovered] = React.useState(false);

  const baseStyle: React.CSSProperties = {
    display: 'inline-flex',
    alignItems: 'center',
    gap: '6px',
    borderRadius: 'var(--radius-input)',
    fontFamily: 'var(--font-sans)',
    fontWeight: 500,
    cursor: disabled || loading ? 'not-allowed' : 'pointer',
    opacity: disabled || loading ? 0.55 : 1,
    transition: 'background-color 120ms, opacity 120ms',
    whiteSpace: 'nowrap',
    ...variantStyles[variant],
    ...sizeStyles[size],
    ...(hovered && !disabled && !loading ? hoverStyles[variant] : {}),
    ...style,
  };

  return (
    <button
      {...props}
      disabled={disabled || loading}
      style={baseStyle}
      onMouseEnter={(e) => { setHovered(true); props.onMouseEnter?.(e); }}
      onMouseLeave={(e) => { setHovered(false); props.onMouseLeave?.(e); }}
    >
      {loading && (
        <span
          style={{
            width: '12px',
            height: '12px',
            border: '2px solid currentColor',
            borderTopColor: 'transparent',
            borderRadius: '50%',
            animation: 'lit-spin 0.6s linear infinite',
            display: 'inline-block',
            flexShrink: 0,
          }}
        />
      )}
      {!loading && Icon && iconPosition === 'left' && (
        <Icon size={size === 'sm' ? 12 : size === 'lg' ? 16 : 14} />
      )}
      {children}
      {!loading && Icon && iconPosition === 'right' && (
        <Icon size={size === 'sm' ? 12 : size === 'lg' ? 16 : 14} />
      )}
    </button>
  );
}
