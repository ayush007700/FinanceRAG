import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  reactStrictMode: true,
  // Static export: the app is client-rendered and talks to the API over HTTP,
  // so it needs no Node server. Emitted to web/out and served from S3 behind
  // CloudFront.
  output: "export",
  // S3 has no directory-index rewriting, so pages are emitted as
  // about/index.html rather than about.html.
  trailingSlash: true,
  images: { unoptimized: true },
};

export default nextConfig;
