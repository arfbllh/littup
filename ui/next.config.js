/** @type {import('next').NextConfig} */
const API_HOST = process.env.API_HOST ?? 'http://localhost:8000';

const nextConfig = {
  async rewrites() {
    return [
      { source: '/api/:path*', destination: `${API_HOST}/api/:path*` },
      { source: '/admin/:path*', destination: `${API_HOST}/admin/:path*` },
    ];
  },
};
module.exports = nextConfig;
