#include <gtk/gtk.h>

typedef struct {
  GtkWidget *window;
  GtkEditable *repo_entry;
  GtkEditable *engine_entry;
  GtkDropDown *policy_dropdown;
  GtkSwitch *offline_switch;
  GtkTextBuffer *output_buffer;
} ForgeUi;

static gchar *app_dir(void) {
  gchar *cwd = g_get_current_dir();
  return cwd;
}

static gchar *core_path(void) {
  gchar *dir = app_dir();
  gchar *path = g_build_filename(dir, "src", "main", "forge-core.mjs", NULL);
  g_free(dir);
  return path;
}

static void set_output(ForgeUi *ui, const gchar *text) {
  gtk_text_buffer_set_text(ui->output_buffer, text ? text : "", -1);
}

static gchar *selected_policy(ForgeUi *ui) {
  guint selected = gtk_drop_down_get_selected(ui->policy_dropdown);
  return g_strdup(selected == 1 ? "atticus" : "default");
}

static GPtrArray *base_args(ForgeUi *ui, const gchar *command) {
  GPtrArray *args = g_ptr_array_new_with_free_func(g_free);
  gchar *core = core_path();
  const gchar *repo = gtk_editable_get_text(ui->repo_entry);
  gchar *policy = selected_policy(ui);
  const gchar *engine = gtk_editable_get_text(ui->engine_entry);
  g_ptr_array_add(args, g_strdup("node"));
  g_ptr_array_add(args, core);
  g_ptr_array_add(args, g_strdup(command));
  g_ptr_array_add(args, g_strdup("--repo"));
  g_ptr_array_add(args, g_strdup(repo && *repo ? repo : "."));
  g_ptr_array_add(args, g_strdup("--policy"));
  g_ptr_array_add(args, policy);
  if (engine && *engine) {
    g_ptr_array_add(args, g_strdup("--engine-command"));
    g_ptr_array_add(args, g_strdup(engine));
  }
  if (gtk_switch_get_active(ui->offline_switch)) {
    g_ptr_array_add(args, g_strdup("--offline-review"));
  }
  g_ptr_array_add(args, NULL);
  return args;
}

static void command_done(GObject *source, GAsyncResult *result, gpointer user_data) {
  ForgeUi *ui = user_data;
  gchar *stdout_text = NULL;
  gchar *stderr_text = NULL;
  GError *error = NULL;
  gboolean ok = g_subprocess_communicate_utf8_finish(G_SUBPROCESS(source), result, &stdout_text, &stderr_text, &error);
  if (!ok) {
    gchar *message = g_strdup_printf("Command failed: %s", error ? error->message : "unknown error");
    set_output(ui, message);
    g_clear_error(&error);
    g_free(message);
  } else {
    gchar *combined = g_strdup_printf("%s%s%s", stdout_text ? stdout_text : "", stderr_text && *stderr_text ? "\n[stderr]\n" : "", stderr_text ? stderr_text : "");
    set_output(ui, combined);
    g_free(combined);
  }
  g_free(stdout_text);
  g_free(stderr_text);
}

static void run_core_command(ForgeUi *ui, const gchar *command) {
  GPtrArray *args = base_args(ui, command);
  GError *error = NULL;
  set_output(ui, "Running...\n");
  GSubprocess *proc = g_subprocess_newv((const gchar *const *)args->pdata, G_SUBPROCESS_FLAGS_STDOUT_PIPE | G_SUBPROCESS_FLAGS_STDERR_PIPE, &error);
  g_ptr_array_free(args, TRUE);
  if (!proc) {
    gchar *message = g_strdup_printf("Failed to launch Forge core: %s", error ? error->message : "unknown error");
    set_output(ui, message);
    g_clear_error(&error);
    g_free(message);
    return;
  }
  g_subprocess_communicate_utf8_async(proc, NULL, NULL, command_done, ui);
  g_object_unref(proc);
}

static void on_status(GtkButton *button, gpointer user_data) { (void)button; run_core_command(user_data, "status"); }
static void on_init(GtkButton *button, gpointer user_data) { (void)button; run_core_command(user_data, "init"); }
static void on_run_one(GtkButton *button, gpointer user_data) { (void)button; run_core_command(user_data, "run-one"); }
static void on_loop(GtkButton *button, gpointer user_data) { (void)button; run_core_command(user_data, "loop"); }
static void on_stop(GtkButton *button, gpointer user_data) { (void)button; run_core_command(user_data, "stop"); }
static void on_resume(GtkButton *button, gpointer user_data) { (void)button; run_core_command(user_data, "resume"); }
static void on_cleanup(GtkButton *button, gpointer user_data) { (void)button; run_core_command(user_data, "cleanup"); }
static void on_dream(GtkButton *button, gpointer user_data) { (void)button; run_core_command(user_data, "dream"); }
static void on_features(GtkButton *button, gpointer user_data) { (void)button; run_core_command(user_data, "features"); }

static GtkWidget *button(const gchar *label, GCallback callback, ForgeUi *ui) {
  GtkWidget *item = gtk_button_new_with_label(label);
  gtk_widget_add_css_class(item, "pill");
  g_signal_connect(item, "clicked", callback, ui);
  return item;
}

static void activate(GtkApplication *app, gpointer user_data) {
  const gchar *initial_repo = user_data;
  ForgeUi *ui = g_new0(ForgeUi, 1);
  GtkCssProvider *css = gtk_css_provider_new();
  gtk_css_provider_load_from_string(css,
    "window { background: #0a0d0c; }"
    ".title { font-size: 42px; font-weight: 800; color: #f5efe1; }"
    ".kicker { color: #d7b46a; letter-spacing: 0.18em; }"
    ".panel { background: rgba(24,25,23,0.94); border: 1px solid rgba(245,239,225,0.16); padding: 18px; }"
    ".pill { background: #1d211f; color: #f5efe1; border: 1px solid rgba(245,239,225,0.20); padding: 10px; }"
    ".accent { background: #ff6b35; color: #15110d; }"
    "textview, entry { background: #080a0a; color: #f5efe1; }"
  );
  gtk_style_context_add_provider_for_display(gdk_display_get_default(), GTK_STYLE_PROVIDER(css), GTK_STYLE_PROVIDER_PRIORITY_APPLICATION);
  g_object_unref(css);

  ui->window = gtk_application_window_new(app);
  gtk_window_set_title(GTK_WINDOW(ui->window), "Atticus Forge");
  gtk_window_set_default_size(GTK_WINDOW(ui->window), 1180, 760);

  GtkWidget *root = gtk_box_new(GTK_ORIENTATION_VERTICAL, 16);
  gtk_widget_set_margin_top(root, 24);
  gtk_widget_set_margin_bottom(root, 24);
  gtk_widget_set_margin_start(root, 24);
  gtk_widget_set_margin_end(root, 24);
  gtk_window_set_child(GTK_WINDOW(ui->window), root);

  GtkWidget *kicker = gtk_label_new("ARCH NATIVE / CLAUDE-CODE-STYLE FORGE LOOP");
  gtk_widget_add_css_class(kicker, "kicker");
  gtk_label_set_xalign(GTK_LABEL(kicker), 0.0f);
  gtk_box_append(GTK_BOX(root), kicker);

  GtkWidget *title = gtk_label_new("Loop control without a web server.");
  gtk_widget_add_css_class(title, "title");
  gtk_label_set_xalign(GTK_LABEL(title), 0.0f);
  gtk_box_append(GTK_BOX(root), title);

  GtkWidget *settings = gtk_grid_new();
  gtk_widget_add_css_class(settings, "panel");
  gtk_grid_set_column_spacing(GTK_GRID(settings), 12);
  gtk_grid_set_row_spacing(GTK_GRID(settings), 10);
  gtk_box_append(GTK_BOX(root), settings);

  GtkWidget *repo_label = gtk_label_new("Target repo");
  gtk_label_set_xalign(GTK_LABEL(repo_label), 0.0f);
  GtkWidget *repo_entry = gtk_entry_new();
  gtk_editable_set_text(GTK_EDITABLE(repo_entry), initial_repo && *initial_repo ? initial_repo : g_get_current_dir());
  ui->repo_entry = GTK_EDITABLE(repo_entry);
  gtk_grid_attach(GTK_GRID(settings), repo_label, 0, 0, 1, 1);
  gtk_grid_attach(GTK_GRID(settings), repo_entry, 1, 0, 3, 1);

  GtkWidget *engine_label = gtk_label_new("Engine command");
  gtk_label_set_xalign(GTK_LABEL(engine_label), 0.0f);
  GtkWidget *engine_entry = gtk_entry_new();
  gtk_editable_set_text(GTK_EDITABLE(engine_entry), "node '/home/alba/open-systeme-Repo 1 Claude Code/openclaw.mjs'");
  ui->engine_entry = GTK_EDITABLE(engine_entry);
  gtk_grid_attach(GTK_GRID(settings), engine_label, 0, 1, 1, 1);
  gtk_grid_attach(GTK_GRID(settings), engine_entry, 1, 1, 3, 1);

  GtkStringList *policies = gtk_string_list_new((const char *[]) { "default", "atticus", NULL });
  ui->policy_dropdown = GTK_DROP_DOWN(gtk_drop_down_new(G_LIST_MODEL(policies), NULL));
  gtk_grid_attach(GTK_GRID(settings), gtk_label_new("Policy"), 0, 2, 1, 1);
  gtk_grid_attach(GTK_GRID(settings), GTK_WIDGET(ui->policy_dropdown), 1, 2, 1, 1);
  ui->offline_switch = GTK_SWITCH(gtk_switch_new());
  gtk_switch_set_active(ui->offline_switch, TRUE);
  gtk_grid_attach(GTK_GRID(settings), gtk_label_new("Offline review"), 2, 2, 1, 1);
  gtk_grid_attach(GTK_GRID(settings), GTK_WIDGET(ui->offline_switch), 3, 2, 1, 1);

  GtkWidget *commands = gtk_flow_box_new();
  gtk_flow_box_set_min_children_per_line(GTK_FLOW_BOX(commands), 3);
  gtk_flow_box_set_max_children_per_line(GTK_FLOW_BOX(commands), 5);
  gtk_flow_box_set_selection_mode(GTK_FLOW_BOX(commands), GTK_SELECTION_NONE);
  gtk_box_append(GTK_BOX(root), commands);
  gtk_flow_box_append(GTK_FLOW_BOX(commands), button("Status", G_CALLBACK(on_status), ui));
  gtk_flow_box_append(GTK_FLOW_BOX(commands), button("Initialize", G_CALLBACK(on_init), ui));
  GtkWidget *run = button("Run One Cycle", G_CALLBACK(on_run_one), ui);
  gtk_widget_add_css_class(run, "accent");
  gtk_flow_box_append(GTK_FLOW_BOX(commands), run);
  gtk_flow_box_append(GTK_FLOW_BOX(commands), button("Start Loop", G_CALLBACK(on_loop), ui));
  gtk_flow_box_append(GTK_FLOW_BOX(commands), button("Stop", G_CALLBACK(on_stop), ui));
  gtk_flow_box_append(GTK_FLOW_BOX(commands), button("Resume", G_CALLBACK(on_resume), ui));
  gtk_flow_box_append(GTK_FLOW_BOX(commands), button("Cleanup", G_CALLBACK(on_cleanup), ui));
  gtk_flow_box_append(GTK_FLOW_BOX(commands), button("Dream Memory", G_CALLBACK(on_dream), ui));
  gtk_flow_box_append(GTK_FLOW_BOX(commands), button("Report Features", G_CALLBACK(on_features), ui));

  GtkWidget *scroll = gtk_scrolled_window_new();
  gtk_widget_set_vexpand(scroll, TRUE);
  gtk_widget_add_css_class(scroll, "panel");
  GtkWidget *output = gtk_text_view_new();
  gtk_text_view_set_monospace(GTK_TEXT_VIEW(output), TRUE);
  gtk_text_view_set_wrap_mode(GTK_TEXT_VIEW(output), GTK_WRAP_WORD_CHAR);
  ui->output_buffer = gtk_text_view_get_buffer(GTK_TEXT_VIEW(output));
  gtk_scrolled_window_set_child(GTK_SCROLLED_WINDOW(scroll), output);
  gtk_box_append(GTK_BOX(root), scroll);
  set_output(ui, "Ready. This GTK app runs local Forge core commands with no web server.\n");

  gtk_window_present(GTK_WINDOW(ui->window));
}

int main(int argc, char **argv) {
  gchar *repo = NULL;
  GPtrArray *app_args = g_ptr_array_new_with_free_func(g_free);
  g_ptr_array_add(app_args, g_strdup(argv[0]));
  for (int i = 1; i < argc; i++) {
    if (g_strcmp0(argv[i], "--repo") == 0 && i + 1 < argc) {
      g_free(repo);
      repo = g_strdup(argv[i + 1]);
      i += 1;
      continue;
    }
    g_ptr_array_add(app_args, g_strdup(argv[i]));
  }
  g_ptr_array_add(app_args, NULL);
  GtkApplication *app = gtk_application_new("local.atticus.forge", G_APPLICATION_DEFAULT_FLAGS);
  g_signal_connect(app, "activate", G_CALLBACK(activate), (gpointer)repo);
  int status = g_application_run(G_APPLICATION(app), (int)app_args->len - 1, (char **)app_args->pdata);
  g_object_unref(app);
  g_ptr_array_free(app_args, TRUE);
  g_free(repo);
  return status;
}
