using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Text;
using MediaBrowser.Model.Logging;
using MediaBrowser.Model.Serialization;

namespace MediaPreviewBridge.Emby.Markers
{
    /// <summary>One JSON file per item, so markers survive restarts and can be written back after a refresh.</summary>
    public class MarkerStore
    {
        /// <summary>
        /// Held across every read, merge and save of an item's store file and chapter rows (API, healer, sweep), so two
        /// of them never interleave and write from stale reads.
        /// </summary>
        public static readonly object Gate = new object();

        // Item id -> version (write time and length) of the unusable store file already warned about, so a file that
        // is read on every item update is logged once.
        private static readonly Dictionary<long, string> Warned = new Dictionary<long, string>();

        private const string TempSuffix = ".tmp";
        private readonly IJsonSerializer _json;
        private readonly ILogger _log;

        public MarkerStore(IJsonSerializer json, ILogger log)
        {
            _json = json;
            _log = log;
        }

        private static string Dir => Plugin.Instance.MarkerStoreDir;

        private static string FileFor(long internalId) =>
            Path.Combine(Dir, internalId.ToString(CultureInfo.InvariantCulture) + ".json");

        /// <summary>Whether a store file exists for the item (cheap check before taking the lock).</summary>
        public bool Exists(long internalId) => File.Exists(FileFor(internalId));

        public void Save(long internalId, StoredMarkers markers)
        {
            lock (Gate)
            {
                Directory.CreateDirectory(Dir);
                var path = FileFor(internalId);
                var tmp = path + TempSuffix;
                var bytes = new UTF8Encoding(false).GetBytes(_json.SerializeToString(markers));
                using (var stream = new FileStream(tmp, FileMode.Create, FileAccess.Write, FileShare.None))
                {
                    stream.Write(bytes, 0, bytes.Length);
                    // On disk before the rename: after a power cut the renamed file must not come back empty.
                    stream.Flush(true);
                }

                // Rename over the old file: there is never a moment without a complete file.
                if (File.Exists(path)) File.Replace(tmp, path, null);
                else File.Move(tmp, path);
                Warned.Remove(internalId);
            }
        }

        /// <summary>
        /// Store <paramref name="markers"/> with the markers a write under way replaces (<paramref name="replacing"/>, see
        /// <see cref="StoredMarkers.Replacing"/>); when neither has markers, delete the item's file.
        /// </summary>
        public void Save(long internalId, StoredMarkers markers, StoredMarkers replacing)
        {
            lock (Gate)
            {
                var stored = StoredMarkers.With(markers, replacing);
                if (stored == null) Delete(internalId);
                else Save(internalId, stored);
            }
        }

        /// <summary>Stored markers, or null when none (a missing, unreadable or invalid file counts as none).</summary>
        public StoredMarkers Load(long internalId)
        {
            lock (Gate)
            {
                var path = FileFor(internalId);
                if (!File.Exists(path)) return null;
                string problem;
                try
                {
                    var text = File.ReadAllText(path);
                    // Emby's reader accepts an object cut off after any complete token, so a file cut inside a number
                    // would read as a different marker (credits at 1 tick). Every file Save writes ends with '}'.
                    if (!text.TrimEnd().EndsWith("}", StringComparison.Ordinal))
                    {
                        problem = "incomplete file";
                    }
                    else
                    {
                        var markers = _json.DeserializeFromString<StoredMarkers>(text);
                        if (StoredMarkers.StoredProblem(markers) == null) return markers;
                        problem = "not a valid marker set";
                    }
                }
                catch (Exception ex)
                {
                    // Best effort: whatever the read or Emby's JSON reader throws (SerializationException,
                    // ArgumentOutOfRangeException, IndexOutOfRangeException... depending on where a file is cut), the
                    // file counts as no markers. Type name only: the message can quote the file's content.
                    problem = ex.GetType().Name;
                }

                WarnOnce(internalId, path, problem);
                return null;
            }
        }

        public bool Delete(long internalId)
        {
            lock (Gate)
            {
                Warned.Remove(internalId);
                var path = FileFor(internalId);
                if (!File.Exists(path)) return false;
                File.Delete(path);
                return true;
            }
        }

        /// <summary>Ids of every item with a store file.</summary>
        public List<long> StoredItemIds()
        {
            lock (Gate)
            {
                if (!Directory.Exists(Dir)) return new List<long>();
                return Directory.EnumerateFiles(Dir, "*.json")
                    .Select(path => long.TryParse(Path.GetFileNameWithoutExtension(path), NumberStyles.None, CultureInfo.InvariantCulture, out var id) ? id : -1)
                    .Where(id => id >= 0)
                    .ToList();
            }
        }

        /// <summary>Delete an item's store file when the item is still gone once the lock is held.</summary>
        public bool DeleteIfGone(long internalId, Func<long, bool> itemExists)
        {
            lock (Gate)
            {
                return !itemExists(internalId) && Delete(internalId);
            }
        }

        /// <summary>Remove temp files a crash left behind mid-save; returns how many.</summary>
        public int DeleteTempFiles()
        {
            lock (Gate)
            {
                if (!Directory.Exists(Dir)) return 0;
                var removed = 0;
                foreach (var tmp in Directory.EnumerateFiles(Dir, "*.json" + TempSuffix).ToList())
                {
                    File.Delete(tmp);
                    removed++;
                }

                return removed;
            }
        }

        private void WarnOnce(long internalId, string path, string problem)
        {
            string version;
            try
            {
                var info = new FileInfo(path);
                version = info.LastWriteTimeUtc.Ticks.ToString(CultureInfo.InvariantCulture) + ":" + info.Length.ToString(CultureInfo.InvariantCulture);
            }
            catch (Exception)
            {
                version = problem;
            }

            if (Warned.TryGetValue(internalId, out var seen) && seen == version) return;
            Warned[internalId] = version;
            _log.Warn("Media Preview Bridge: ignoring marker file for item {0}: {1}", internalId, problem);
        }
    }
}
