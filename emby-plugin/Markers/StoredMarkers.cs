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

        /// <summary>Why these markers can't be stored, or null when they can.</summary>
        public static string Problem(StoredMarkers m)
        {
            if (m == null) return "body is required";
            if (m.IntroStartTicks.HasValue != m.IntroEndTicks.HasValue) return "intro start and end must be sent together";
            if (m.IntroStartTicks.HasValue && (m.IntroStartTicks.Value < 0 || m.IntroEndTicks.Value <= m.IntroStartTicks.Value))
                return "invalid intro ticks";
            if (m.CreditsStartTicks.HasValue && m.CreditsStartTicks.Value < 0) return "invalid credits ticks";
            if (m.FileSize.HasValue && m.FileSize.Value <= 0) return "invalid fileSize";
            if (!m.IntroStartTicks.HasValue && !m.CreditsStartTicks.HasValue) return "no markers; use DELETE to clear";
            return null;
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
