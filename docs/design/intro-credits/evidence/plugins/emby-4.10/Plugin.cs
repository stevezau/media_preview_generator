using System;
using System.Collections.Generic;
using System.Linq;
using MediaBrowser.Common.Configuration;
using MediaBrowser.Common.Plugins;
using MediaBrowser.Controller.Library;
using MediaBrowser.Controller.Net;
using MediaBrowser.Controller.Persistence;
using MediaBrowser.Model.Entities;
using MediaBrowser.Model.Plugins;
using MediaBrowser.Model.Serialization;
using MediaBrowser.Model.Services;
using MediaBrowser.Controller.Plugins;
using MediaBrowser.Model.Logging;
using System.IO;

namespace MarkersLabEmby
{
    public class PluginConfiguration : BasePluginConfiguration { }
    public class Plugin : BasePlugin<PluginConfiguration>
    {
        public Plugin(IApplicationPaths p, IXmlSerializer x) : base(p, x) { Instance = this; }
        public static Plugin Instance { get; private set; }
        public string StoreDir => Path.Combine(DataFolderPath, "markers");
        public override string Name => "Markers Lab Emby";
        public override Guid Id => new Guid("c2cb9bf9-7c5d-4f1a-9a07-2d6f5e5b00bb");
    }

    [Route("/markerslab/set", "POST")]
    [Authenticated(Roles = "admin")]
    public class SetMarkers : IReturn<object>
    {
        public long Id { get; set; }
        public long? IntroStart { get; set; }
        public long? IntroEnd { get; set; }
        public long? CreditsStart { get; set; }
    }

    public class MarkersService : IService
    {
        private readonly ILibraryManager _lib;
        private readonly IItemRepository _repo;
        public MarkersService(ILibraryManager lib, IItemRepository repo) { _lib = lib; _repo = repo; }

        public object Post(SetMarkers req)
        {
            var item = _lib.GetItemById(req.Id);
            if (item == null) throw new ArgumentException("not found");
            var chapters = (_repo.GetChapters(item) ?? new List<ChapterInfo>())
                .Where(c => c.MarkerType == MarkerType.Chapter).ToList();
            if (req.IntroStart.HasValue) chapters.Add(new ChapterInfo { Name = "Intro", StartPositionTicks = req.IntroStart.Value, MarkerType = MarkerType.IntroStart });
            if (req.IntroEnd.HasValue) chapters.Add(new ChapterInfo { Name = "Intro End", StartPositionTicks = req.IntroEnd.Value, MarkerType = MarkerType.IntroEnd });
            if (req.CreditsStart.HasValue) chapters.Add(new ChapterInfo { Name = "Credits", StartPositionTicks = req.CreditsStart.Value, MarkerType = MarkerType.CreditsStart });
            chapters = chapters.OrderBy(c => c.StartPositionTicks).ToList();
            _repo.SaveChapters(item.InternalId, chapters);
            Directory.CreateDirectory(Plugin.Instance.StoreDir);
            File.WriteAllText(Path.Combine(Plugin.Instance.StoreDir, item.InternalId + ".txt"), $"{req.IntroStart}|{req.IntroEnd}|{req.CreditsStart}");
            return new { item.InternalId, count = chapters.Count };
        }
    }

    public class Healer : IServerEntryPoint
    {
        private readonly ILibraryManager _lib; private readonly IItemRepository _repo; private readonly ILogger _log;
        public Healer(ILibraryManager lib, IItemRepository repo, ILogManager lm) { _lib = lib; _repo = repo; _log = lm.GetLogger("MarkersLabHealer"); }
        public void Run() { _lib.ItemUpdated += OnUpdated; }
        public void Dispose() { _lib.ItemUpdated -= OnUpdated; }
        private void OnUpdated(object sender, ItemChangeEventArgs e)
        {
            try {
                var f = Path.Combine(Plugin.Instance.StoreDir, e.Item.InternalId + ".txt");
                if (!File.Exists(f)) return;
                var cur = _repo.GetChapters(e.Item) ?? new List<ChapterInfo>();
                _log.Info("ItemUpdated {0} reason={1} markers={2}", e.Item.InternalId, e.UpdateReason, cur.Count(c => c.MarkerType != MarkerType.Chapter));
                if (cur.Any(c => c.MarkerType != MarkerType.Chapter)) return;
                var p = File.ReadAllText(f).Split('|');
                var list = cur.Where(c => c.MarkerType == MarkerType.Chapter).ToList();
                if (p[0] != "") list.Add(new ChapterInfo { Name = "Intro", StartPositionTicks = long.Parse(p[0]), MarkerType = MarkerType.IntroStart });
                if (p[1] != "") list.Add(new ChapterInfo { Name = "Intro End", StartPositionTicks = long.Parse(p[1]), MarkerType = MarkerType.IntroEnd });
                if (p[2] != "") list.Add(new ChapterInfo { Name = "Credits", StartPositionTicks = long.Parse(p[2]), MarkerType = MarkerType.CreditsStart });
                _repo.SaveChapters(e.Item.InternalId, list.OrderBy(c => c.StartPositionTicks).ToList());
                _log.Info("Re-applied markers to {0}", e.Item.InternalId);
            } catch (Exception ex) { _log.ErrorException("heal failed", ex); }
        }
    }
}
