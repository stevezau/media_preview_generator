using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using MediaBrowser.Controller.Entities;
using MediaBrowser.Model.Entities;

namespace MediaPreviewBridge.Emby.Markers
{
    /// <summary>
    /// Emby keeps markers as chapter rows with a MarkerType. The plugin only ever removes rows equal to the ones it
    /// wrote for the stored markers (or for the markers a write under way replaces); the file's chapters and markers
    /// from Emby or other plugins stay unless the caller asks for ours to replace a type.
    /// </summary>
    public static class MarkerChapters
    {
        private static readonly MarkerType[] IntroTypes = { MarkerType.IntroStart, MarkerType.IntroEnd };
        private static readonly MarkerType[] CreditsTypes = { MarkerType.CreditsStart };

        /// <summary>The chapter rows this plugin writes for <paramref name="markers"/> (none when null).</summary>
        public static List<ChapterInfo> RowsFor(StoredMarkers markers)
        {
            var rows = new List<ChapterInfo>();
            if (markers == null) return rows;
            if (markers.IntroStartTicks.HasValue)
            {
                rows.Add(new ChapterInfo { Name = "Intro", StartPositionTicks = markers.IntroStartTicks.Value, MarkerType = MarkerType.IntroStart });
                rows.Add(new ChapterInfo { Name = "Intro End", StartPositionTicks = markers.IntroEndTicks.Value, MarkerType = MarkerType.IntroEnd });
            }

            if (markers.CreditsStartTicks.HasValue)
            {
                rows.Add(new ChapterInfo { Name = "Credits", StartPositionTicks = markers.CreditsStartTicks.Value, MarkerType = MarkerType.CreditsStart });
            }

            return rows;
        }

        /// <summary>
        /// <paramref name="existing"/> without the rows written for <paramref name="ours"/> or for the markers it was
        /// replacing (<see cref="StoredMarkers.Replacing"/>), plus the rows for <paramref name="wanted"/>, by start.
        /// Intro (start + end) and credits are handled as types: a type that still has someone else's rows keeps them and
        /// gets none of ours, unless <paramref name="replaceOthers"/>, which swaps those rows for ours.
        /// </summary>
        /// <param name="written">How many of our rows the result holds for <paramref name="wanted"/>.</param>
        public static List<ChapterInfo> Apply(IEnumerable<ChapterInfo> existing, StoredMarkers ours, StoredMarkers wanted, bool replaceOthers, out int written)
        {
            var ourRows = RowsFor(ours).Concat(RowsFor(ours?.Replacing)).ToList();
            var list = (existing ?? Enumerable.Empty<ChapterInfo>()).Where(c => !ourRows.Any(r => SameRow(c, r))).ToList();
            var wantedRows = RowsFor(wanted);
            written = AddType(list, wantedRows, IntroTypes, replaceOthers) + AddType(list, wantedRows, CreditsTypes, replaceOthers);
            return list.OrderBy(c => c.StartPositionTicks).ToList();
        }

        /// <summary>
        /// What <paramref name="existing"/> still shows of <paramref name="stored"/> or of the markers it was replacing,
        /// per type, or null when no row of either is there. A write saves this beside its new markers before it writes
        /// the rows, so a stop in between leaves no row of ours the store doesn't name.
        /// </summary>
        public static StoredMarkers ShownOf(IEnumerable<ChapterInfo> existing, StoredMarkers stored)
        {
            var rows = (existing ?? Enumerable.Empty<ChapterInfo>()).ToList();
            var shown = new StoredMarkers();
            foreach (var set in new[] { stored, stored?.Replacing })
            {
                var setRows = RowsFor(set);
                if (!shown.IntroStartTicks.HasValue && HasRowOf(rows, setRows, IntroTypes))
                {
                    shown.IntroStartTicks = set.IntroStartTicks;
                    shown.IntroEndTicks = set.IntroEndTicks;
                }

                if (!shown.CreditsStartTicks.HasValue && HasRowOf(rows, setRows, CreditsTypes))
                {
                    shown.CreditsStartTicks = set.CreditsStartTicks;
                }
            }

            return StoredMarkers.HasMarkers(shown) ? shown : null;
        }

        /// <summary>Whether both lists hold the same rows (type, start and name), in any order.</summary>
        public static bool SameRows(IEnumerable<ChapterInfo> a, IEnumerable<ChapterInfo> b) =>
            Keys(a).SequenceEqual(Keys(b));

        /// <summary>The item's file size on disk, or null when it can't be read.</summary>
        public static long? CurrentFileSize(BaseItem item)
        {
            try
            {
                return string.IsNullOrEmpty(item.Path) || !File.Exists(item.Path) ? (long?)null : new FileInfo(item.Path).Length;
            }
            catch (Exception)
            {
                // Best effort: an unreadable size only means the size can't be compared.
                return null;
            }
        }

        private static int AddType(List<ChapterInfo> list, List<ChapterInfo> wantedRows, MarkerType[] types, bool replaceOthers)
        {
            var rows = wantedRows.Where(r => types.Contains(r.MarkerType)).ToList();
            if (rows.Count == 0) return 0;
            if (list.Any(c => types.Contains(c.MarkerType)))
            {
                if (!replaceOthers) return 0;
                list.RemoveAll(c => types.Contains(c.MarkerType));
            }

            list.AddRange(rows);
            return rows.Count;
        }

        private static bool HasRowOf(List<ChapterInfo> rows, List<ChapterInfo> setRows, MarkerType[] types) =>
            setRows.Any(r => types.Contains(r.MarkerType) && rows.Any(c => SameRow(c, r)));

        private static bool SameRow(ChapterInfo a, ChapterInfo b) =>
            a.MarkerType == b.MarkerType && a.StartPositionTicks == b.StartPositionTicks && string.Equals(a.Name, b.Name, StringComparison.Ordinal);

        private static List<string> Keys(IEnumerable<ChapterInfo> rows) =>
            (rows ?? Enumerable.Empty<ChapterInfo>())
                .Select(c => string.Join("|", c.StartPositionTicks, (int)c.MarkerType, c.Name))
                .OrderBy(k => k, StringComparer.Ordinal)
                .ToList();
    }
}
