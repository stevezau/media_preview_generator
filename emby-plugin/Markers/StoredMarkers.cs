using System;

namespace MediaPreviewBridge.Emby.Markers
{
    /// <summary>What Media Preview Generator sent for one item (ticks). Emby has no credits end.</summary>
    public class StoredMarkers
    {
        public long? IntroStartTicks { get; set; }

        public long? IntroEndTicks { get; set; }

        public long? CreditsStartTicks { get; set; }

        /// <summary>Gets or sets the size in bytes of the file the markers were detected on (null: not sent).</summary>
        public long? FileSize { get; set; }

        /// <summary>Gets or sets the item's path when the markers were stored (null in files that don't have it).</summary>
        public string Path { get; set; }

        /// <summary>
        /// Gets or sets, only while a write is under way, the markers of ours the item's rows showed before it (null
        /// otherwise). The store is saved before the chapter rows, so if Emby stops in between, the rows can still be
        /// either set: both count as ours until a POST or DELETE clears it, or an item update finishes the write (only
        /// while the file is still the one the markers were sent for; a replaced file keeps it until the next POST or
        /// DELETE).
        /// </summary>
        public StoredMarkers Replacing { get; set; }

        /// <summary>Why these markers can't be stored, or null when they can.</summary>
        public static string Problem(StoredMarkers m)
        {
            if (m == null) return "body is required";
            if (m.IntroStartTicks.HasValue != m.IntroEndTicks.HasValue) return "intro start and end must be sent together";
            if (m.IntroStartTicks.HasValue && (m.IntroStartTicks.Value < 0 || m.IntroEndTicks.Value <= m.IntroStartTicks.Value))
                return "invalid intro ticks";
            if (m.CreditsStartTicks.HasValue && m.CreditsStartTicks.Value < 0) return "invalid credits ticks";
            if (m.FileSize.HasValue && m.FileSize.Value <= 0) return "invalid fileSize";
            if (!HasMarkers(m)) return "no markers; use DELETE to clear";
            return null;
        }

        /// <summary>
        /// Why a store file's content can't be used, or null when it can: a valid set, with or without the valid set it
        /// replaces, or (a DELETE under way) only the set it replaces.
        /// </summary>
        public static string StoredProblem(StoredMarkers m)
        {
            if (m?.Replacing == null) return Problem(m);
            if (m.Replacing.Replacing != null || Problem(m.Replacing) != null) return "invalid replaced markers";
            return (HasMarkers(m) || m.IntroEndTicks.HasValue) ? Problem(m) : null;
        }

        /// <summary>Whether the set holds an intro or credits.</summary>
        public static bool HasMarkers(StoredMarkers m) => m != null && (m.IntroStartTicks.HasValue || m.CreditsStartTicks.HasValue);

        /// <summary>
        /// A copy of <paramref name="markers"/> (no markers when null) holding <paramref name="replacing"/>, or null when
        /// neither has markers (nothing to store).
        /// </summary>
        public static StoredMarkers With(StoredMarkers markers, StoredMarkers replacing)
        {
            if (!HasMarkers(markers) && !HasMarkers(replacing)) return null;
            return new StoredMarkers
            {
                IntroStartTicks = markers?.IntroStartTicks,
                IntroEndTicks = markers?.IntroEndTicks,
                CreditsStartTicks = markers?.CreditsStartTicks,
                FileSize = markers?.FileSize,
                Path = markers?.Path,
                Replacing = HasMarkers(replacing) ? replacing : null,
            };
        }

        /// <summary>
        /// Whether the markers belong to a different file than the item has now: the item's path changed, or the file
        /// on disk has a different size (it was replaced). Unknown values on either side don't count as a difference.
        /// </summary>
        public static bool IsStale(StoredMarkers m, long? currentFileSize, string currentPath) =>
            m != null
            && ((m.FileSize.HasValue && currentFileSize.HasValue && m.FileSize.Value != currentFileSize.Value)
                || (!string.IsNullOrEmpty(m.Path) && !string.IsNullOrEmpty(currentPath) && !string.Equals(m.Path, currentPath, StringComparison.Ordinal)));
    }
}
