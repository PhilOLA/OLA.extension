# -*- coding: utf-8 -*-
"""Turn off ALL Annotation Categories for selected Revit Links
across selected View Templates in the current model.

Usage:
    Drop this file as script.py inside a pyRevit pushbutton folder:
    MyTools.tab/Links.panel/LinkAnnotations.pushbutton/script.py
    Reload pyRevit and click it. All inputs are gathered from the
    dialog window that appears.
"""

import json

from pyrevit import revit, DB
from pyrevit import script
from pyrevit import forms

import clr
clr.AddReference("PresentationFramework")
from System.Windows import Visibility
from System.Windows.Threading import DispatcherPriority, Dispatcher

doc = revit.doc
output = script.get_output()
config = script.get_config()

SAVED_TEMPLATES_CONFIG_KEY = "saved_link_annotation_templates"

# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------

def load_saved_template_names():
    """Return list of previously selected view template names."""
    raw = config.get_option(SAVED_TEMPLATES_CONFIG_KEY, "[]")
    try:
        return json.loads(raw)
    except Exception:
        return []


def save_template_names(names):
    config.set_option(SAVED_TEMPLATES_CONFIG_KEY, json.dumps(list(names)))
    script.save_config()


# ---------------------------------------------------------------------------
# Data collection helpers
# ---------------------------------------------------------------------------

def get_view_templates():
    """All view templates in the project, sorted by name."""
    views = DB.FilteredElementCollector(doc).OfClass(DB.View).ToElements()
    templates = [v for v in views if v.IsTemplate]
    templates.sort(key=lambda v: v.Name)
    return templates


def get_revit_link_types():
    """Return list of dicts with keys: type_name, type_id, instances.
    Each entry represents one RvtLinkType and all its RevitLinkInstance elements."""
    link_types = DB.FilteredElementCollector(doc)\
        .OfClass(DB.RevitLinkType).ToElements()
    link_instances = DB.FilteredElementCollector(doc)\
        .OfClass(DB.RevitLinkInstance).ToElements()

    # Map type id -> instances
    type_to_instances = {}
    for inst in link_instances:
        type_id = inst.GetTypeId().IntegerValue
        type_to_instances.setdefault(type_id, []).append(inst)

    result = []
    for lt in link_types:
        tid = lt.Id.IntegerValue
        insts = type_to_instances.get(tid, [])
        if not insts:
            continue  # skip unloaded types with no instances
        try:
            type_name = lt.Name
        except Exception:
            type_name = "Link [id:{}]".format(tid)
        result.append({
            "type_name": type_name,
            "type_id": tid,
            "instances": insts,
        })

    result.sort(key=lambda x: x["type_name"])
    return result


def get_instance_display_name(inst):
    """Return a meaningful display name for a single RevitLinkInstance."""
    try:
        name = inst.Name
        if name and name.strip():
            return name.strip()
    except Exception:
        pass
    return "Instance [id:{}]".format(inst.Id.IntegerValue)


# ---------------------------------------------------------------------------
# SelectableItem helper (mirrors PlaceViews pattern)
# ---------------------------------------------------------------------------

class SelectableItem(object):
    def __init__(self, name, checked=False, tag=None):
        self.Name = name
        self.IsChecked = checked
        self.Tag = tag  # stores the underlying Revit element or dict


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------

def get_annotation_categories(host_doc):
    """Return all annotation Category objects from the host document
    that allow visibility control in views."""
    cats = []
    try:
        for cat in host_doc.Settings.Categories:
            try:
                if cat.CategoryType == DB.CategoryType.Annotation:
                    cats.append(cat)
            except Exception:
                pass
    except Exception:
        pass
    return cats


def apply_link_annotation_override(template, link_instance, annotation_cats):
    """For one view template + one link instance: set Custom link display
    settings then hide each annotation category individually in the template.

    The Revit 2024 API does not expose a single 'hide all annotation
    categories in link' toggle on RevitLinkGraphicsSettings.  The UI checkbox
    'Show annotation categories in this view' maps internally to setting
    AreAnnotationCategoriesHidden on a ByLinkView-referenced view - which
    requires a real view inside the linked doc.  The equivalent per-template
    approach is to:
      1. Set the link to Custom display via RevitLinkGraphicsSettings.
      2. Hide every annotation category in the VIEW TEMPLATE itself
         (which governs what is drawn, including from links, when the
         template controls VG).
    Returns (success, error_message)."""
    try:
        link_id = link_instance.Id

        # Step 1 — ensure Custom link display settings
        overrides = template.GetLinkOverrides(link_id)
        if overrides is None:
            overrides = DB.RevitLinkGraphicsSettings()
        if overrides.LinkVisibilityType != DB.LinkVisibility.Custom:
            overrides.LinkVisibilityType = DB.LinkVisibility.Custom
        template.SetLinkOverrides(link_id, overrides)

        # Step 2 — hide every annotation category in the view template
        hidden_count = 0
        for cat in annotation_cats:
            try:
                if template.CanCategoryBeHidden(cat.Id):
                    template.SetCategoryHidden(cat.Id, True)
                    hidden_count += 1
            except Exception:
                pass

        return True, None

    except Exception as e:
        return False, str(e)


# ---------------------------------------------------------------------------
# XAML definitions
# ---------------------------------------------------------------------------

MESSAGE_DIALOG_XAML = """
<Window
    xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation"
    xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml"
    Title="Notice"
    SizeToContent="Height" Width="440" MaxHeight="640"
    WindowStartupLocation="CenterScreen"
    ResizeMode="NoResize"
    Background="#FF262626"
    FontFamily="Segoe UI">
    <Window.Resources>
        <Style TargetType="TextBlock">
            <Setter Property="Foreground" Value="#FFE6E6E6"/>
        </Style>
        <Style TargetType="Button">
            <Setter Property="Background" Value="#FF0A84D8"/>
            <Setter Property="Foreground" Value="White"/>
            <Setter Property="Padding" Value="14,7"/>
            <Setter Property="BorderThickness" Value="0"/>
            <Setter Property="Margin" Value="6,0,0,0"/>
            <Setter Property="Cursor" Value="Hand"/>
            <Setter Property="FontSize" Value="12"/>
            <Setter Property="MinWidth" Value="70"/>
        </Style>
        <Style x:Key="SecondaryButton" TargetType="Button" BasedOn="{StaticResource {x:Type Button}}">
            <Setter Property="Background" Value="#FF4A4A4A"/>
        </Style>
    </Window.Resources>
    <Grid Margin="20">
        <Grid.RowDefinitions>
            <RowDefinition Height="Auto"/>
            <RowDefinition Height="Auto"/>
        </Grid.RowDefinitions>
        <ScrollViewer Grid.Row="0" VerticalScrollBarVisibility="Auto" MaxHeight="500">
            <TextBlock x:Name="txt_message" Text="" FontSize="13" TextWrapping="Wrap" Margin="0,0,0,20"/>
        </ScrollViewer>
        <StackPanel Grid.Row="1" Orientation="Horizontal" HorizontalAlignment="Right">
            <Button x:Name="btn_secondary" Content="No" Click="btn_secondary_click"
                    Style="{StaticResource SecondaryButton}" Visibility="Collapsed"/>
            <Button x:Name="btn_primary" Content="OK" Click="btn_primary_click"/>
        </StackPanel>
    </Grid>
</Window>
"""


FORM_XAML = """
<Window
    xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation"
    xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml"
    Title="Link Annotation Visibility"
    Height="740" Width="820"
    MinHeight="560" MinWidth="680"
    WindowStartupLocation="CenterScreen"
    ResizeMode="CanResize"
    Background="#FF262626"
    FontFamily="Segoe UI">

    <Window.Resources>
        <Style TargetType="TextBlock">
            <Setter Property="Foreground" Value="#FFE6E6E6"/>
        </Style>
        <Style x:Key="SectionLabel" TargetType="TextBlock">
            <Setter Property="Foreground" Value="#FFC8C8C8"/>
            <Setter Property="FontSize" Value="11"/>
            <Setter Property="FontWeight" Value="SemiBold"/>
            <Setter Property="Margin" Value="0,0,0,4"/>
        </Style>
        <Style TargetType="TextBox">
            <Setter Property="Background" Value="#FF3A3A3A"/>
            <Setter Property="Foreground" Value="White"/>
            <Setter Property="Padding" Value="6,5"/>
            <Setter Property="BorderBrush" Value="#FF4F4F4F"/>
            <Setter Property="CaretBrush" Value="White"/>
        </Style>
        <Style TargetType="CheckBox">
            <Setter Property="Foreground" Value="#FFE6E6E6"/>
            <Setter Property="Margin" Value="0,5,0,0"/>
        </Style>
        <Style TargetType="ListBox">
            <Setter Property="Background" Value="#FF1E1E1E"/>
            <Setter Property="BorderBrush" Value="#FF4F4F4F"/>
            <Setter Property="Foreground" Value="#FFE6E6E6"/>
        </Style>
        <Style TargetType="Button">
            <Setter Property="Background" Value="#FF0A84D8"/>
            <Setter Property="Foreground" Value="White"/>
            <Setter Property="Padding" Value="14,7"/>
            <Setter Property="BorderThickness" Value="0"/>
            <Setter Property="Margin" Value="6,0,0,0"/>
            <Setter Property="Cursor" Value="Hand"/>
            <Setter Property="FontSize" Value="12"/>
            <Setter Property="MinWidth" Value="80"/>
        </Style>
        <Style x:Key="SecondaryButton" TargetType="Button" BasedOn="{StaticResource {x:Type Button}}">
            <Setter Property="Background" Value="#FF4A4A4A"/>
        </Style>
        <Style x:Key="SmallButton" TargetType="Button" BasedOn="{StaticResource SecondaryButton}">
            <Setter Property="Padding" Value="8,3"/>
            <Setter Property="FontSize" Value="10"/>
            <Setter Property="MinWidth" Value="0"/>
        </Style>
        <Style x:Key="WarningBanner" TargetType="Border">
            <Setter Property="Background" Value="#FF2A2000"/>
            <Setter Property="BorderBrush" Value="#FFFFB74D"/>
            <Setter Property="BorderThickness" Value="0,0,0,0"/>
            <Setter Property="CornerRadius" Value="3"/>
            <Setter Property="Padding" Value="12,10"/>
            <Setter Property="Margin" Value="0,10,0,10"/>
        </Style>
    </Window.Resources>

    <Grid Margin="16">
        <Grid.RowDefinitions>
            <RowDefinition Height="Auto"/>
            <RowDefinition Height="Auto"/>
            <RowDefinition Height="*"/>
            <RowDefinition Height="Auto"/>
            <RowDefinition Height="Auto"/>
            <RowDefinition Height="Auto"/>
            <RowDefinition Height="Auto"/>
        </Grid.RowDefinitions>

        <!-- Title -->
        <StackPanel Grid.Row="0" Margin="0,0,0,12">
            <TextBlock Text="Link Annotation Visibility" FontSize="17" FontWeight="Bold"/>
            <TextBlock Text="Select view templates and linked models to update below."
                       Foreground="#FF9B9B9B" FontSize="11" Margin="0,3,0,0"/>
        </StackPanel>

        <!-- Warning banner -->
        <Border Grid.Row="1" Style="{StaticResource WarningBanner}">
            <StackPanel Orientation="Horizontal">
                <TextBlock Text="&#9888;  " Foreground="#FFFFB74D" FontSize="15" FontWeight="Bold"
                           VerticalAlignment="Center"/>
                <TextBlock TextWrapping="Wrap" VerticalAlignment="Center">
                    <Run Text="ALL Annotation Categories will be hidden" Foreground="#FFFFB74D"
                         FontWeight="Bold" FontSize="13"/>
                    <Run Text=" for the selected Revit Links across all selected View Templates."
                         Foreground="#FFDDDDDD" FontSize="12"/>
                </TextBlock>
            </StackPanel>
        </Border>

        <!-- Two list panels side by side -->
        <Grid Grid.Row="2">
            <Grid.ColumnDefinitions>
                <ColumnDefinition Width="*"/>
                <ColumnDefinition Width="14"/>
                <ColumnDefinition Width="*"/>
            </Grid.ColumnDefinitions>

            <!-- LEFT: View Templates -->
            <Grid Grid.Column="0">
                <Grid.RowDefinitions>
                    <RowDefinition Height="Auto"/>
                    <RowDefinition Height="Auto"/>
                    <RowDefinition Height="Auto"/>
                    <RowDefinition Height="Auto"/>
                    <RowDefinition Height="*"/>
                </Grid.RowDefinitions>

                <TextBlock Grid.Row="0" Text="VIEW TEMPLATES" Style="{StaticResource SectionLabel}"/>

                <TextBox Grid.Row="1" x:Name="txt_template_filter" Margin="0,0,0,4"
                         TextChanged="txt_template_filter_changed"/>

                <CheckBox Grid.Row="2" x:Name="chk_templates_selected_only"
                          Content="Show selected only"
                          Checked="chk_templates_selected_only_changed"
                          Unchecked="chk_templates_selected_only_changed"/>

                <Grid Grid.Row="3" Margin="0,6,0,4">
                    <Grid.ColumnDefinitions>
                        <ColumnDefinition Width="Auto"/>
                        <ColumnDefinition Width="Auto"/>
                        <ColumnDefinition Width="*"/>
                    </Grid.ColumnDefinitions>
                    <Button Grid.Column="0" Content="All" Click="btn_templates_all_click"
                            Style="{StaticResource SmallButton}" Margin="0,0,4,0"/>
                    <Button Grid.Column="1" Content="None" Click="btn_templates_none_click"
                            Style="{StaticResource SmallButton}" Margin="0,0,0,0"/>
                    <TextBlock Grid.Column="2" x:Name="txt_template_count"
                               Foreground="#FF9B9B9B" FontSize="11"
                               HorizontalAlignment="Right" VerticalAlignment="Center"
                               Text="0 selected"/>
                </Grid>

                <ListBox Grid.Row="4" x:Name="lst_templates" MinHeight="200"
                         PreviewMouseUp="lst_templates_mouse_up">
                    <ListBox.ItemTemplate>
                        <DataTemplate>
                            <CheckBox Content="{Binding Name}" IsChecked="{Binding IsChecked, Mode=TwoWay}"/>
                        </DataTemplate>
                    </ListBox.ItemTemplate>
                </ListBox>
            </Grid>

            <!-- RIGHT: Linked Models -->
            <Grid Grid.Column="2">
                <Grid.RowDefinitions>
                    <RowDefinition Height="Auto"/>
                    <RowDefinition Height="Auto"/>
                    <RowDefinition Height="Auto"/>
                    <RowDefinition Height="Auto"/>
                    <RowDefinition Height="*"/>
                </Grid.RowDefinitions>

                <TextBlock Grid.Row="0" Text="LINKED MODELS" Style="{StaticResource SectionLabel}"/>

                <TextBox Grid.Row="1" x:Name="txt_link_filter" Margin="0,0,0,4"
                         TextChanged="txt_link_filter_changed"/>

                <StackPanel Grid.Row="2" Orientation="Horizontal">
                    <CheckBox x:Name="chk_links_selected_only"
                              Content="Show selected only"
                              Checked="chk_links_selected_only_changed"
                              Unchecked="chk_links_selected_only_changed"
                              Margin="0,5,12,0"/>
                    <CheckBox x:Name="chk_show_instances"
                              Content="Show instance names"
                              Checked="chk_show_instances_changed"
                              Unchecked="chk_show_instances_changed"
                              Margin="0,5,0,0"/>
                </StackPanel>

                <Grid Grid.Row="3" Margin="0,6,0,4">
                    <Grid.ColumnDefinitions>
                        <ColumnDefinition Width="Auto"/>
                        <ColumnDefinition Width="Auto"/>
                        <ColumnDefinition Width="*"/>
                    </Grid.ColumnDefinitions>
                    <Button Grid.Column="0" Content="All" Click="btn_links_all_click"
                            Style="{StaticResource SmallButton}" Margin="0,0,4,0"/>
                    <Button Grid.Column="1" Content="None" Click="btn_links_none_click"
                            Style="{StaticResource SmallButton}" Margin="0,0,0,0"/>
                    <TextBlock Grid.Column="2" x:Name="txt_link_count"
                               Foreground="#FF9B9B9B" FontSize="11"
                               HorizontalAlignment="Right" VerticalAlignment="Center"
                               Text="0 selected"/>
                </Grid>

                <ListBox Grid.Row="4" x:Name="lst_links" MinHeight="200"
                         PreviewMouseUp="lst_links_mouse_up">
                    <ListBox.ItemTemplate>
                        <DataTemplate>
                            <CheckBox Content="{Binding Name}" IsChecked="{Binding IsChecked, Mode=TwoWay}"/>
                        </DataTemplate>
                    </ListBox.ItemTemplate>
                </ListBox>
            </Grid>
        </Grid>

        <!-- Progress bar (hidden until Run) -->
        <Grid Grid.Row="3" x:Name="pnl_progress" Visibility="Collapsed" Margin="0,10,0,0">
            <Grid.RowDefinitions>
                <RowDefinition Height="Auto"/>
                <RowDefinition Height="Auto"/>
            </Grid.RowDefinitions>
            <TextBlock Grid.Row="0" x:Name="txt_progress_label" Text="Applying overrides..."
                       Foreground="#FF9B9B9B" FontSize="11" Margin="0,0,0,4"/>
            <ProgressBar Grid.Row="1" x:Name="prg_bar" Height="8" Minimum="0" Maximum="100"
                         Value="0" Background="#FF3A3A3A" Foreground="#FF0A84D8"
                         BorderBrush="#FF4F4F4F"/>
        </Grid>

        <!-- Error text -->
        <TextBlock Grid.Row="4" x:Name="txt_error" Foreground="#FFFF6B6B" FontSize="11"
                   Margin="0,8,0,0" TextWrapping="Wrap" Visibility="Collapsed"/>

        <!-- Status label -->
        <TextBlock Grid.Row="5" x:Name="txt_status" Foreground="#FF9B9B9B" FontSize="11"
                   Margin="0,8,0,0" TextWrapping="Wrap" Text="Ready."/>

        <!-- Buttons -->
        <StackPanel Grid.Row="6" Orientation="Horizontal" HorizontalAlignment="Right" Margin="0,12,0,0">
            <Button x:Name="btn_close" Content="Close" Click="btn_close_click"
                    Style="{StaticResource SecondaryButton}"/>
            <Button x:Name="btn_run" Content="Apply Overrides" Click="btn_run_click"/>
        </StackPanel>
    </Grid>
</Window>
"""


# ---------------------------------------------------------------------------
# MessageDialog (matches existing apps)
# ---------------------------------------------------------------------------

class MessageDialog(forms.WPFWindow):
    def __init__(self, xaml_source, message, title="Notice",
                 primary_text="OK", secondary_text=None):
        forms.WPFWindow.__init__(self, xaml_source, literal_string=True)
        self.Title = title
        self.txt_message.Text = message
        self.btn_primary.Content = primary_text
        self.result = None
        if secondary_text:
            self.btn_secondary.Content = secondary_text
            self.btn_secondary.Visibility = Visibility.Visible

    def btn_primary_click(self, sender, args):
        self.result = True
        self.Close()

    def btn_secondary_click(self, sender, args):
        self.result = False
        self.Close()


def show_info(message, title="Notice"):
    dlg = MessageDialog(MESSAGE_DIALOG_XAML, message, title=title, primary_text="OK")
    dlg.ShowDialog()


def show_confirm(message, title="Confirm", yes_text="Yes", no_text="No"):
    dlg = MessageDialog(MESSAGE_DIALOG_XAML, message, title=title,
                        primary_text=yes_text, secondary_text=no_text)
    dlg.ShowDialog()
    return bool(dlg.result)


# ---------------------------------------------------------------------------
# Main form
# ---------------------------------------------------------------------------

class LinkAnnotationsForm(forms.WPFWindow):

    def __init__(self, xaml_source, view_templates, link_type_dicts, saved_template_names):
        forms.WPFWindow.__init__(self, xaml_source, literal_string=True)

        self._all_templates = view_templates
        self._link_type_dicts = link_type_dicts  # list of dicts from get_revit_link_types()
        self._show_instances = False

        # --- View Template items ---
        saved = set(saved_template_names)
        self.template_items = [
            SelectableItem(v.Name, checked=(v.Name in saved), tag=v)
            for v in view_templates
        ]

        # --- Link items (type-level by default) ---
        self.link_type_items = []   # one per type
        self.link_inst_items = []   # one per instance
        self._build_link_items()

        self.refresh_template_list()
        self.refresh_link_list()

    # -----------------------------------------------------------------------
    # Link item builders
    # -----------------------------------------------------------------------

    def _build_link_items(self):
        self.link_type_items = []
        self.link_inst_items = []
        for d in self._link_type_dicts:
            type_item = SelectableItem(d["type_name"], checked=False, tag=d)
            self.link_type_items.append(type_item)
            for inst in d["instances"]:
                inst_item = SelectableItem(
                    get_instance_display_name(inst), checked=False, tag=inst
                )
                self.link_inst_items.append(inst_item)

    def _active_link_items(self):
        return self.link_inst_items if self._show_instances else self.link_type_items

    # -----------------------------------------------------------------------
    # List refresh helpers
    # -----------------------------------------------------------------------

    def refresh_template_list(self, filter_text=""):
        ft = (filter_text or "").lower()
        show_sel = bool(self.chk_templates_selected_only.IsChecked)
        visible = [
            item for item in self.template_items
            if ft in item.Name.lower() and (not show_sel or item.IsChecked)
        ]
        self.lst_templates.ItemsSource = visible
        self._update_template_count()

    def refresh_link_list(self, filter_text=""):
        ft = (filter_text or "").lower()
        show_sel = bool(self.chk_links_selected_only.IsChecked)
        visible = [
            item for item in self._active_link_items()
            if ft in item.Name.lower() and (not show_sel or item.IsChecked)
        ]
        self.lst_links.ItemsSource = visible
        self._update_link_count()

    def _update_template_count(self):
        count = sum(1 for i in self.template_items if i.IsChecked)
        self.txt_template_count.Text = "{} selected".format(count)

    def _update_link_count(self):
        count = sum(1 for i in self._active_link_items() if i.IsChecked)
        self.txt_link_count.Text = "{} selected".format(count)

    # -----------------------------------------------------------------------
    # Error / status helpers
    # -----------------------------------------------------------------------

    def show_error(self, message):
        self.txt_error.Text = message
        self.txt_error.Visibility = Visibility.Visible

    def clear_error(self):
        self.txt_error.Text = ""
        self.txt_error.Visibility = Visibility.Collapsed

    def set_status(self, message):
        self.txt_status.Text = message
        self.txt_status.Dispatcher.Invoke(
            DispatcherPriority.Background, System_Action(lambda: None))

    # -----------------------------------------------------------------------
    # Event handlers — templates
    # -----------------------------------------------------------------------

    def txt_template_filter_changed(self, sender, args):
        self.refresh_template_list(self.txt_template_filter.Text)

    def chk_templates_selected_only_changed(self, sender, args):
        self.refresh_template_list(self.txt_template_filter.Text)

    def lst_templates_mouse_up(self, sender, args):
        if bool(self.chk_templates_selected_only.IsChecked):
            self.refresh_template_list(self.txt_template_filter.Text)
        else:
            self._update_template_count()

    def btn_templates_all_click(self, sender, args):
        for item in list(self.lst_templates.ItemsSource):
            item.IsChecked = True
        self.refresh_template_list(self.txt_template_filter.Text)

    def btn_templates_none_click(self, sender, args):
        for item in list(self.lst_templates.ItemsSource):
            item.IsChecked = False
        self.refresh_template_list(self.txt_template_filter.Text)

    # -----------------------------------------------------------------------
    # Event handlers — links
    # -----------------------------------------------------------------------

    def txt_link_filter_changed(self, sender, args):
        self.refresh_link_list(self.txt_link_filter.Text)

    def chk_links_selected_only_changed(self, sender, args):
        self.refresh_link_list(self.txt_link_filter.Text)

    def chk_show_instances_changed(self, sender, args):
        self._show_instances = bool(self.chk_show_instances.IsChecked)
        # When switching modes, carry checked state across by type name matching
        if self._show_instances:
            checked_type_names = set(
                i.Tag["type_name"] for i in self.link_type_items if i.IsChecked
            )
            for inst_item in self.link_inst_items:
                inst = inst_item.Tag
                # find which type this instance belongs to
                for d in self._link_type_dicts:
                    if inst in d["instances"]:
                        inst_item.IsChecked = d["type_name"] in checked_type_names
                        break
        else:
            checked_inst_type_ids = set()
            for inst_item in self.link_inst_items:
                if inst_item.IsChecked:
                    inst = inst_item.Tag
                    for d in self._link_type_dicts:
                        if inst in d["instances"]:
                            checked_inst_type_ids.add(d["type_id"])
                            break
            for type_item in self.link_type_items:
                type_item.IsChecked = type_item.Tag["type_id"] in checked_inst_type_ids
        self.refresh_link_list(self.txt_link_filter.Text)

    def lst_links_mouse_up(self, sender, args):
        if bool(self.chk_links_selected_only.IsChecked):
            self.refresh_link_list(self.txt_link_filter.Text)
        else:
            self._update_link_count()

    def btn_links_all_click(self, sender, args):
        for item in list(self.lst_links.ItemsSource):
            item.IsChecked = True
        self.refresh_link_list(self.txt_link_filter.Text)

    def btn_links_none_click(self, sender, args):
        for item in list(self.lst_links.ItemsSource):
            item.IsChecked = False
        self.refresh_link_list(self.txt_link_filter.Text)

    # -----------------------------------------------------------------------
    # Collect selected instances from either mode
    # -----------------------------------------------------------------------

    def _selected_instances(self):
        """Return flat list of selected RevitLinkInstance elements."""
        instances = []
        if self._show_instances:
            for item in self.link_inst_items:
                if item.IsChecked:
                    instances.append(item.Tag)
        else:
            for type_item in self.link_type_items:
                if type_item.IsChecked:
                    for inst in type_item.Tag["instances"]:
                        instances.append(inst)
        return instances

    def _selected_templates(self):
        return [item.Tag for item in self.template_items if item.IsChecked]

    # -----------------------------------------------------------------------
    # Run / Close
    # -----------------------------------------------------------------------

    def btn_close_click(self, sender, args):
        self.Close()

    def btn_run_click(self, sender, args):
        self.clear_error()

        selected_templates = self._selected_templates()
        selected_instances = self._selected_instances()

        if not selected_templates:
            self.show_error("Please select at least one View Template.")
            return
        if not selected_instances:
            self.show_error("Please select at least one Linked Model.")
            return

        total = len(selected_templates) * len(selected_instances)
        confirm_msg = (
            u"This will hide ALL Annotation Categories for:\n\n"
            u"  \u2022 {} view template(s)\n"
            u"  \u2022 {} link instance(s)\n\n"
            u"{} override(s) will be applied.\n\n"
            u"Continue?"
        ).format(len(selected_templates), len(selected_instances), total)

        if not show_confirm(confirm_msg, title="Confirm Override", yes_text="Apply", no_text="Cancel"):
            return

        # Save template selection for next time
        save_template_names([t.Name for t in selected_templates])

        # Show progress bar
        self.pnl_progress.Visibility = Visibility.Visible
        self.btn_run.IsEnabled = False
        self.btn_close.IsEnabled = False

        errors = []
        done = 0

        # Collect annotation categories once before the transaction
        annotation_cats = get_annotation_categories(doc)

        try:
            with revit.Transaction("Hide Link Annotation Categories in View Templates"):
                for tmpl in selected_templates:
                    for inst in selected_instances:
                        self.txt_progress_label.Text = (
                            u"Applying: {} ({}/{})".format(
                                tmpl.Name, done + 1, total)
                        )
                        pct = int(done * 100.0 / total)
                        self.prg_bar.Value = pct
                        # Force UI update mid-loop
                        self.prg_bar.Dispatcher.Invoke(
                            DispatcherPriority.Background,
                            System_Action(lambda: None)
                        )

                        ok, err = apply_link_annotation_override(tmpl, inst, annotation_cats)
                        if not ok:
                            errors.append(
                                u"{} / {}: {}".format(
                                    tmpl.Name, get_instance_display_name(inst), err)
                            )
                        done += 1

            self.prg_bar.Value = 100

        except Exception as ex:
            self.show_error("Transaction failed: {}".format(ex))
            self.pnl_progress.Visibility = Visibility.Collapsed
            self.btn_run.IsEnabled = True
            self.btn_close.IsEnabled = True
            return

        # Summary
        succeeded = total - len(errors)
        if errors:
            summary = (
                u"Complete with errors.\n\n"
                u"{} of {} override(s) applied successfully.\n\n"
                u"Failed:\n{}".format(
                    succeeded, total,
                    u"\n".join(u"  - " + e for e in errors))
            )
        else:
            summary = (
                u"Done!\n\n"
                u"All {} override(s) applied successfully.\n\n"
                u"Annotation Categories have been hidden for {} link instance(s) "
                u"across {} view template(s).".format(
                    total, len(selected_instances), len(selected_templates))
            )

        self.txt_progress_label.Text = "Complete."
        self.prg_bar.Value = 100
        self.txt_status.Text = "{} of {} override(s) applied.".format(succeeded, total)
        self.btn_run.IsEnabled = True
        self.btn_close.IsEnabled = True

        show_info(summary, title="Batch Complete")


# ---------------------------------------------------------------------------
# System.Action shim (needed for Dispatcher.Invoke from IronPython)
# ---------------------------------------------------------------------------

import System
System_Action = System.Action


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    view_templates = get_view_templates()
    if not view_templates:
        show_info("No view templates were found in this project.", title="Nothing to do")
        return

    link_type_dicts = get_revit_link_types()
    if not link_type_dicts:
        show_info("No loaded Revit Links were found in this project.", title="Nothing to do")
        return

    saved_names = load_saved_template_names()

    dlg = LinkAnnotationsForm(FORM_XAML, view_templates, link_type_dicts, saved_names)
    dlg.ShowDialog()


main()
