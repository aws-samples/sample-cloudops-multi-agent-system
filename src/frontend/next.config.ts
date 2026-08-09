import type { NextConfig } from "next";

const nextConfig: NextConfig = {
    output: "export",
    trailingSlash: true,
    devIndicators: false,
    // Next 16's dev server generates AGENTS.md/CLAUDE.md into the app dir on every
    // run. CLAUDE.md is gitignored repo-wide but AGENTS.md would land as untracked
    // noise in every contributor's tree — disable the generation instead.
    agentRules: false,
};

export default nextConfig;
