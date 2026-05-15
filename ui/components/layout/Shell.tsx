'use client';

import React from 'react';
import Link from 'next/link';
import { usePathname } from 'next/navigation';
import { FileText, FileEdit, Settings, ExternalLink } from 'lucide-react';
import useSWR from 'swr';
import { getLlmStats } from '@/lib/api';

const NAV_ITEMS = [
  { label: 'Documents', href: '/documents', icon: FileText },
  { label: 'Drafts', href: '/drafts', icon: FileEdit },
  { label: 'Admin', href: '/admin', icon: Settings },
];

interface ShellProps {
  children: React.ReactNode;
}

export function Shell({ children }: ShellProps) {
  const pathname = usePathname();
  const { data: stats } = useSWR('llm-stats', getLlmStats, {
    refreshInterval: 15_000,
    onErrorRetry: () => {},
  });

  const spendRatio = stats
    ? stats.budget.current_spend_usd / (stats.budget.hourly_usd || 1)
    : 0;
  const spendPct = Math.min(spendRatio * 100, 100);
  const isHighSpend = spendPct > 80;

  function isActive(href: string): boolean {
    if (href === '/documents') return pathname.startsWith('/documents');
    if (href === '/drafts') return pathname.startsWith('/drafts');
    if (href === '/admin') return pathname.startsWith('/admin');
    return false;
  }

  return (
    <div
      style={{
        display: 'flex',
        flexDirection: 'column',
        minHeight: '100vh',
        backgroundColor: 'var(--paper-50)',
      }}
    >
      {/* Top bar */}
      <header
        style={{
          height: '44px',
          borderBottom: '1px solid var(--paper-300)',
          backgroundColor: '#fff',
          display: 'flex',
          alignItems: 'center',
          padding: '0 16px',
          gap: '16px',
          flexShrink: 0,
          zIndex: 10,
        }}
      >
        {/* Wordmark */}
        <Link
          href="/documents"
          style={{
            textDecoration: 'none',
            display: 'flex',
            alignItems: 'baseline',
            gap: '0',
          }}
        >
          <span
            style={{
              fontFamily: 'var(--font-sans)',
              fontSize: '16px',
              fontWeight: 400,
              color: 'var(--paper-800)',
              letterSpacing: '-0.01em',
            }}
          >
            litt
          </span>
          <span
            style={{
              fontFamily: 'var(--font-sans)',
              fontSize: '16px',
              fontWeight: 700,
              color: 'var(--ochre-500)',
              letterSpacing: '-0.01em',
            }}
          >
            UP
          </span>
        </Link>

        {/* Workspace label */}
        <span
          style={{
            fontFamily: 'var(--font-mono)',
            fontSize: '11px',
            color: 'var(--paper-400)',
            backgroundColor: 'var(--paper-100)',
            padding: '2px 8px',
            borderRadius: 'var(--radius-pill)',
            border: '1px solid var(--paper-300)',
          }}
        >
          workspace:demo
        </span>

        <div style={{ flex: 1 }} />

        {/* Spend badge */}
        {stats && (
          <span
            style={{
              fontFamily: 'var(--font-mono)',
              fontSize: '11px',
              color: isHighSpend ? 'var(--ochre-600)' : 'var(--paper-400)',
              backgroundColor: isHighSpend ? 'var(--ochre-50)' : 'var(--paper-100)',
              padding: '2px 8px',
              borderRadius: 'var(--radius-pill)',
              border: `1px solid ${isHighSpend ? 'var(--ochre-100)' : 'var(--paper-300)'}`,
            }}
          >
            ${stats.budget.current_spend_usd.toFixed(2)}/h
            {isHighSpend && ' ▲'}
          </span>
        )}

        {/* Demo link */}
        <a
          href="https://github.com/arfbllh/littup"
          target="_blank"
          rel="noopener noreferrer"
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: '4px',
            fontSize: '12px',
            color: 'var(--paper-400)',
            textDecoration: 'none',
            fontFamily: 'var(--font-sans)',
          }}
        >
          <ExternalLink size={12} />
          Repo
        </a>
      </header>

      {/* Main area */}
      <div style={{ display: 'flex', flex: 1, minHeight: 0 }}>
        {/* Left nav rail */}
        <nav
          style={{
            width: '200px',
            flexShrink: 0,
            borderRight: '1px solid var(--paper-300)',
            backgroundColor: '#fff',
            display: 'flex',
            flexDirection: 'column',
            padding: '16px 0',
          }}
        >
          <div className="eyebrow" style={{ padding: '0 16px 10px' }}>
            Workspace
          </div>

          {NAV_ITEMS.map(({ label, href, icon: Icon }) => {
            const active = isActive(href);
            return (
              <Link
                key={href}
                href={href}
                style={{
                  display: 'flex',
                  alignItems: 'center',
                  gap: '8px',
                  padding: '8px 16px',
                  fontSize: '13px',
                  fontFamily: 'var(--font-sans)',
                  fontWeight: active ? 600 : 400,
                  color: active ? 'var(--ochre-700)' : 'var(--paper-600)',
                  backgroundColor: active ? 'var(--ochre-50)' : 'transparent',
                  textDecoration: 'none',
                  borderLeft: active ? '2px solid var(--ochre-500)' : '2px solid transparent',
                  transition: 'background-color 100ms',
                }}
              >
                <Icon size={14} />
                {label}
              </Link>
            );
          })}

          <div style={{ flex: 1 }} />

          {/* Active template footer */}
          <div
            style={{
              margin: '12px',
              padding: '8px 10px',
              backgroundColor: 'var(--paper-100)',
              borderRadius: 'var(--radius-card)',
              border: '1px solid var(--paper-200)',
            }}
          >
            <div className="eyebrow" style={{ marginBottom: '4px' }}>
              Active Template
            </div>
            <div
              style={{
                fontSize: '12px',
                color: 'var(--paper-600)',
                fontFamily: 'var(--font-sans)',
              }}
            >
              Demand Letter v1
            </div>
          </div>
        </nav>

        {/* Page content */}
        <main style={{ flex: 1, minWidth: 0, display: 'flex', flexDirection: 'column' }}>
          {children}
        </main>
      </div>

      {/* Status bar */}
      <footer
        style={{
          height: '28px',
          borderTop: '1px solid var(--paper-300)',
          backgroundColor: '#fff',
          display: 'flex',
          alignItems: 'center',
          padding: '0 16px',
          gap: '16px',
          flexShrink: 0,
        }}
      >
        {/* API connected dot */}
        <span
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: '5px',
            fontFamily: 'var(--font-mono)',
            fontSize: '11px',
            color: 'var(--paper-400)',
          }}
        >
          <span
            style={{
              width: '6px',
              height: '6px',
              borderRadius: '50%',
              backgroundColor: stats ? 'var(--cite-supported)' : 'var(--paper-300)',
            }}
          />
          api{stats ? '·connected' : '·?'}
        </span>

        {stats && (
          <>
            <span
              style={{
                fontFamily: 'var(--font-mono)',
                fontSize: '11px',
                color: 'var(--paper-400)',
              }}
            >
              calls {stats.totals.calls}
            </span>
            <span
              style={{
                fontFamily: 'var(--font-mono)',
                fontSize: '11px',
                color: 'var(--paper-400)',
              }}
            >
              cache {Math.round(stats.totals.cache_hit_rate * 100)}%
            </span>
            <span
              style={{
                fontFamily: 'var(--font-mono)',
                fontSize: '11px',
                color: 'var(--paper-400)',
              }}
            >
              ${stats.budget.current_spend_usd.toFixed(2)}/${stats.budget.hourly_usd.toFixed(2)}/h
            </span>
          </>
        )}

        <div style={{ flex: 1 }} />
        <span
          style={{
            fontFamily: 'var(--font-mono)',
            fontSize: '11px',
            color: 'var(--paper-300)',
          }}
        >
          littup · m11
        </span>
      </footer>
    </div>
  );
}
