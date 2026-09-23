using System;
using System.IO;
using MediaBrowser.Common.Configuration;
using MediaBrowser.Common.Plugins;
using MediaBrowser.Model.Plugins;
using MediaBrowser.Model.Serialization;

namespace MediaPreviewBridge.Emby
{
    /// <summary>No settings: everything is driven by Media Preview Generator.</summary>
    public class PluginConfiguration : BasePluginConfiguration
    {
    }

    /// <summary>Receives Skip Intro / Skip Credits markers and keeps them through Emby's metadata refreshes.</summary>
    public class Plugin : BasePlugin<PluginConfiguration>
    {
        /// <summary>Plugin id; never change it (Emby keys the install on it).</summary>
        public static readonly Guid PluginId = new Guid("8d6c1b3e-2f4a-4c5d-9e7f-0a1b2c3d4e5f");

        /// <summary>Initializes a new instance of the <see cref="Plugin"/> class.</summary>
        public Plugin(IApplicationPaths applicationPaths, IXmlSerializer xmlSerializer)
            : base(applicationPaths, xmlSerializer)
        {
            Instance = this;
        }

        /// <summary>Gets the running instance.</summary>
        public static Plugin Instance { get; private set; }

        /// <inheritdoc />
        public override string Name => "Media Preview Bridge for Emby";

        /// <inheritdoc />
        public override string Description =>
            "Shows Skip Intro and Skip Credits markers sent by Media Preview Generator, and puts them back when a metadata refresh removes them.";

        /// <inheritdoc />
        public override Guid Id => PluginId;

        /// <summary>Gets the folder holding one JSON file per item with markers.</summary>
        public string MarkerStoreDir => Path.Combine(DataFolderPath, "markers");
    }
}
