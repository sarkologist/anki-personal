// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import { bridgeCommand, bridgeCommandsAvailable } from "@tslib/bridgecommand";

export type LegacyLatexKind = "inline" | "display";

export interface LegacyLatexPreviewRequest {
    requestId: string;
    kind: LegacyLatexKind;
    html: string;
}

export type LegacyLatexPreviewResult =
    | {
        requestId: string;
        ok: true;
        dataUrl: string;
        alt: string;
        svg: boolean;
    }
    | {
        requestId: string;
        ok: false;
        errorText: string;
    };

type LegacyLatexPreviewBridgeResponse =
    | {
        status: "pending";
    }
    | {
        status: "ready";
        result: LegacyLatexPreviewResult;
    };

type PreviewResolver = (result: LegacyLatexPreviewResult) => void;

const pending = new Map<string, PreviewResolver>();
let nextRequestId = 0;

function unavailableResult(requestId: string): LegacyLatexPreviewResult {
    return {
        requestId,
        ok: false,
        errorText: "LaTeX preview unavailable",
    };
}

function receiveLatexPreviewResult(result: LegacyLatexPreviewResult): void {
    const resolver = pending.get(result.requestId);

    if (!resolver) {
        return;
    }

    pending.delete(result.requestId);
    resolver(result);
}

(globalThis as typeof globalThis & {
    receiveLatexPreviewResult?: typeof receiveLatexPreviewResult;
}).receiveLatexPreviewResult = receiveLatexPreviewResult;

export function requestLegacyLatexPreview(
    kind: LegacyLatexKind,
    html: string,
): Promise<LegacyLatexPreviewResult> {
    const requestId = String(++nextRequestId);
    const request: LegacyLatexPreviewRequest = { requestId, kind, html };

    if (!bridgeCommandsAvailable()) {
        return Promise.resolve(unavailableResult(requestId));
    }

    return new Promise((resolve) => {
        pending.set(requestId, resolve);
        bridgeCommand<LegacyLatexPreviewBridgeResponse>(
            `renderLatexPreview:${JSON.stringify(request)}`,
            (response) => {
                if (response.status === "ready") {
                    pending.delete(requestId);
                    resolve(response.result);
                }
            },
        );
    });
}
