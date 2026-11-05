export {};

declare global {
    interface DocumentOrShadowRoot {
        getSelection(): Selection | null;
    }

    interface GetComposedRangesOptions {
        shadowRoots?: ShadowRoot[];
    }

    /** Not yet in TypeScript's DOM lib */
    interface Selection {
        getComposedRanges?(options?: GetComposedRangesOptions): StaticRange[];
        /** Safari 17.x */
        getComposedRanges?(...shadowRoots: ShadowRoot[]): StaticRange[];
    }

    interface Node {
        getRootNode(options?: GetRootNodeOptions): Document | ShadowRoot;
    }
}
