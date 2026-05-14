import React from 'react';

interface MonoProps {
  children: React.ReactNode;
  style?: React.CSSProperties;
  className?: string;
}

export function Mono({ children, style, className }: MonoProps) {
  return (
    <span
      className={`mono-id ${className ?? ''}`}
      style={style}
    >
      {children}
    </span>
  );
}
