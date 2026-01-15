"""
BlenderJPS Operators
Operators for loading JuPedSim trajectory and geometry data.
"""

import bpy
from bpy.types import Operator
from bpy.props import StringProperty
from bpy_extras.io_utils import ImportHelper
import pathlib
import threading
import time
import traceback
from array import array


BIG_DATA_STATE = {
    "positions": None,
    "min_frame": 0,
    "max_frame": 0,
    "object_name": None,
    "handler_installed": False,
}


def _big_data_frame_handler(scene):
    """Update big data mesh vertices for the current frame."""
    state = BIG_DATA_STATE
    if not state["positions"] or not state["object_name"]:
        return
    obj = bpy.data.objects.get(state["object_name"])
    if not obj or obj.type != 'MESH':
        return
    frame = scene.frame_current
    if frame < state["min_frame"] or frame > state["max_frame"]:
        return
    frame_idx = frame - state["min_frame"]
    positions = state["positions"][frame_idx]
    if positions is None:
        return
    obj.data.vertices.foreach_set("co", positions)
    obj.data.update()


def check_dependencies():
    """Check if required dependencies are installed."""
    try:
        import pedpy
        return True, None
    except ImportError as e:
        return False, str(e)


class JUPEDSIM_OT_select_file(Operator, ImportHelper):
    """Select a JuPedSim SQLite trajectory file."""
    
    bl_idname = "jupedsim.select_file"
    bl_label = "Select SQLite File"
    bl_description = "Browse for a JuPedSim trajectory SQLite file"
    
    filter_glob: StringProperty(
        default="*.sqlite;*.db",
        options={'HIDDEN'},
    )
    
    def execute(self, context):
        """Store the selected SQLite file path in the scene properties."""
        context.scene.jupedsim_props.sqlite_file = self.filepath
        self.report({'INFO'}, f"Selected file: {self.filepath}")
        return {'FINISHED'}


class JUPEDSIM_OT_load_simulation(Operator):
    """Load simulation data from the selected SQLite file."""
    
    bl_idname = "jupedsim.load_simulation"
    bl_label = "Load Simulation"
    bl_description = "Load agent trajectories and geometry from the SQLite file"
    bl_options = {'REGISTER', 'UNDO'}
    
    _timer = None
    _worker_thread = None
    _worker_done = False
    _worker_error = None
    _worker_data = None
    _worker_timings = None
    _worker_traceback = None
    _cancel_event = None
    _cancelled = False
    _timings = None
    _agent_groups = None
    _agent_index = 0
    _path_index = 0
    _frame_step = 1
    _min_frame = 0
    _max_frame = 0
    _sampled_frames = None
    _agents_collection = None
    _geometry_collection = None
    _total_agents = 0
    _stage = None
    _big_data_mode = False
    
    def execute(self, context):
        """Start a modal load that keeps the UI responsive."""
        # Check dependencies first
        deps_ok, error = check_dependencies()
        if not deps_ok:
            self.report({'ERROR'}, f"Missing dependencies: {error}")
            self.report({'ERROR'}, "Please install dependencies in addon preferences.")
            return {'CANCELLED'}
        
        props = context.scene.jupedsim_props
        if props.loading_in_progress:
            self.report({'WARNING'}, "A load is already in progress")
            return {'CANCELLED'}
        
        # Get file path
        filepath = props.sqlite_file
        if not filepath:
            self.report({'ERROR'}, "No SQLite file selected")
            return {'CANCELLED'}
        
        filepath = bpy.path.abspath(filepath)
        path = pathlib.Path(filepath)
        
        if not path.exists():
            self.report({'ERROR'}, f"File not found: {filepath}")
            return {'CANCELLED'}
        
        self._reset_state()
        self._frame_step = props.frame_step
        self._big_data_mode = props.big_data_mode
        self._cancel_event = threading.Event()
        props.loading_in_progress = True
        props.loading_progress = 0.0
        props.loading_message = "Starting load..."
        self._stage = "loading_sqlite"

        # Start worker thread for SQLite loading
        self._worker_thread = threading.Thread(
            target=self._load_sqlite_worker,
            args=(path, self._frame_step, self._cancel_event),
            daemon=True,
        )
        self._worker_thread.start()

        wm = context.window_manager
        self._timer = wm.event_timer_add(0.1, window=context.window)
        wm.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        """Advance loading stages and update progress."""
        props = context.scene.jupedsim_props

        if event.type == 'ESC':
            self._cancelled = True
            if self._cancel_event:
                self._cancel_event.set()
            props.loading_message = "Cancelling..."
            return self._finish_cancel(context)

        if event.type != 'TIMER':
            return {'RUNNING_MODAL'}

        if self._cancelled:
            return self._finish_cancel(context)

        # Update progress while waiting for worker
        if self._stage == "loading_sqlite":
            props.loading_message = "Loading trajectory data..."
            props.loading_progress = 5.0
            if self._worker_done:
                if self._worker_error:
                    self.report({'ERROR'}, f"Failed to load simulation: {self._worker_error}")
                    self._print_worker_traceback()
                    return self._finish_cancel(context)
                self._apply_worker_data(context)
                props.loading_message = "Trajectory loaded"
                props.loading_progress = 20.0
                self._stage = "create_collections"

        if self._stage == "create_collections":
            self._timed_start("create_collections")
            self._agents_collection = self._get_or_create_collection("JuPedSim_Agents")
            self._geometry_collection = self._get_or_create_collection("JuPedSim_Geometry")
            self._timed_end("create_collections")
            props.loading_message = "Preparing scene..."
            props.loading_progress = 10.0
            self._stage = "create_geometry"

        if self._stage == "create_geometry":
            self._timed_start("create_geometry")
            self._create_geometry(context, self._worker_data["geometry"], self._geometry_collection)
            self._timed_end("create_geometry")
            props.loading_message = "Creating geometry..."
            props.loading_progress = 25.0
            self._stage = "create_big_data" if self._big_data_mode else "create_agents"

        if self._stage == "create_agents":
            props.loading_message = "Creating agents..."
            if self._step_create_agents(context):
                self._timed_end("create_agents")
                self._stage = "create_paths"

        if self._stage == "create_big_data":
            props.loading_message = "Building frame buffers..."
            props.loading_progress = 60.0
            self._timed_start("create_big_data")
            self._create_big_data_points(context)
            self._timed_end("create_big_data")
            props.loading_message = "Creating particle points..."
            props.loading_progress = 90.0
            self._stage = "finalize"

        if self._stage == "create_paths":
            props.loading_message = "Creating agent paths..."
            if self._step_create_paths(context):
                self._timed_end("create_paths")
                self._stage = "finalize"

        if self._stage == "finalize":
            self._timed_start("finalize")
            show_paths = props.show_paths
            self._update_path_visibility(self._agents_collection, show_paths)
            self._timed_end("finalize")
            props.loading_progress = 100.0
            props.loading_message = "Load complete"
            self._log_timings()
            self.report({'INFO'}, "Simulation loaded successfully!")
            return self._finish_success(context)

        return {'RUNNING_MODAL'}

    def _reset_state(self):
        """Reset modal state for a new load."""
        self._worker_thread = None
        self._worker_done = False
        self._worker_error = None
        self._worker_data = None
        self._worker_timings = None
        self._worker_traceback = None
        self._cancel_event = None
        self._cancelled = False
        self._timings = {}
        self._agent_groups = None
        self._agent_index = 0
        self._path_index = 0
        self._frame_step = 1
        self._min_frame = 0
        self._max_frame = 0
        self._sampled_frames = None
        self._agents_collection = None
        self._geometry_collection = None
        self._total_agents = 0
        self._stage = None
        self._big_data_mode = False
        self._clear_big_data_state()

    def _finish_success(self, context):
        """Finalize a successful load."""
        self._cleanup_timer(context)
        props = context.scene.jupedsim_props
        props.loading_in_progress = False
        return {'FINISHED'}

    def _finish_cancel(self, context):
        """Finalize a cancelled load while keeping partial data."""
        self._cleanup_timer(context)
        self._finalize_timings_on_cancel()
        self._log_timings()
        props = context.scene.jupedsim_props
        props.loading_in_progress = False
        props.loading_message = "Load cancelled (partial data kept)"
        return {'CANCELLED'}

    def _cleanup_timer(self, context):
        """Remove the modal timer."""
        if self._timer:
            context.window_manager.event_timer_remove(self._timer)
            self._timer = None

    def _finalize_timings_on_cancel(self):
        for key, value in (self._worker_timings or {}).items():
            if key not in self._timings:
                self._timings[key] = value
        for key, value in list(self._timings.items()):
            if value < 0:
                self._timings[key] = time.perf_counter() + value
                print(f"[BlenderJPS] {key} took {self._timings[key]:.3f}s (cancelled)")

    def _load_sqlite_worker(self, path, frame_step, cancel_event):
        """Load data in a worker thread to keep UI responsive."""
        from pedpy import (
            load_trajectory_from_jupedsim_sqlite,
            load_walkable_area_from_jupedsim_sqlite,
        )
        try:
            timings = {}
            start_total = time.perf_counter()

            start = time.perf_counter()
            traj_data = load_trajectory_from_jupedsim_sqlite(trajectory_file=path)
            timings["load_trajectory_sqlite"] = time.perf_counter() - start
            if cancel_event.is_set():
                self._worker_timings = timings
                self._worker_done = True
                return

            start = time.perf_counter()
            geometry = load_walkable_area_from_jupedsim_sqlite(trajectory_file=path)
            timings["load_geometry_sqlite"] = time.perf_counter() - start
            if cancel_event.is_set():
                self._worker_timings = timings
                self._worker_done = True
                return

            start = time.perf_counter()
            df = traj_data.data
            agent_groups = list(df.groupby('id'))
            frames = sorted(df['frame'].unique())
            min_frame = int(frames[0]) if frames else 0
            max_frame = int(frames[-1]) if frames else 0
            sampled_frames = set(frames[::frame_step]) if frames else set()
            timings["prepare_agent_groups"] = time.perf_counter() - start

            frame_positions = None
            agent_ids = None
            if self._big_data_mode:
                start = time.perf_counter()
                frame_positions, agent_ids = self._build_frame_positions_from_df(
                    df, min_frame, max_frame, frame_step
                )
                timings["build_frame_positions"] = time.perf_counter() - start
                print(
                    "[BlenderJPS] build frame positions "
                    f"(frames={len(frame_positions)}, agents={len(agent_ids)}) "
                    f"took {timings['build_frame_positions']:.3f}s"
                )

            timings["load_sqlite_total"] = time.perf_counter() - start_total

            self._worker_data = {
                "geometry": geometry,
                "agent_groups": agent_groups,
                "min_frame": min_frame,
                "max_frame": max_frame,
                "sampled_frames": sampled_frames,
                "frame_positions": frame_positions,
                "agent_ids": agent_ids,
            }
            self._worker_timings = timings
            self._worker_done = True
        except Exception as e:
            self._worker_error = str(e)
            self._worker_traceback = traceback.format_exc()
            self._worker_done = True

    def _build_frame_positions_from_df(
        self, df, min_frame, max_frame, frame_step
    ):
        """Build per-frame vertex arrays for big data mode from a DataFrame."""
        agent_ids = sorted(df['id'].unique())
        agent_index = {agent_id: idx for idx, agent_id in enumerate(agent_ids)}
        frame_count = max_frame - min_frame + 1
        hide_z = -1.0e6
        positions_by_frame = []
        for _ in range(frame_count):
            frame_array = array('f', [0.0] * (len(agent_ids) * 3))
            for i in range(2, len(frame_array), 3):
                frame_array[i] = hide_z
            positions_by_frame.append(frame_array)

        x_col = 'x' if 'x' in df.columns else 'pos_x'
        y_col = 'y' if 'y' in df.columns else 'pos_y'

        for row in df[[x_col, y_col, 'frame', 'id']].itertuples(index=False):
            x, y, frame, agent_id = row
            if frame_step > 1 and frame % frame_step != 0:
                continue
            frame_idx = int(frame) - min_frame
            if frame_idx < 0 or frame_idx >= frame_count:
                continue
            idx = agent_index[agent_id]
            base_offset = idx * 3
            arr = positions_by_frame[frame_idx]
            arr[base_offset] = float(x)
            arr[base_offset + 1] = float(y)
            arr[base_offset + 2] = 0.5

        return positions_by_frame, agent_ids

    def _apply_worker_data(self, context):
        """Apply worker results to the modal state."""
        self._agent_groups = self._worker_data["agent_groups"]
        self._total_agents = len(self._agent_groups)
        self._min_frame = self._worker_data["min_frame"]
        self._max_frame = self._worker_data["max_frame"]
        self._sampled_frames = self._worker_data["sampled_frames"]
        context.scene.jupedsim_props.loaded_agent_count = self._total_agents
        context.scene.frame_start = self._min_frame
        context.scene.frame_end = self._max_frame
        for key, value in (self._worker_timings or {}).items():
            self._timings[key] = value
        if not self._big_data_mode:
            self._timed_start("create_agents")

    def _step_create_agents(self, context):
        """Create a small batch of agent objects per tick."""
        if self._agent_groups is None:
            return True
        if self._agent_index == 0:
            self.report({'INFO'}, f"Creating {self._total_agents} agents (every {self._frame_step} frame(s))...")
        chunk_size = 10
        start = self._agent_index
        end = min(self._total_agents, start + chunk_size)
        for idx in range(start, end):
            agent_id, agent_data = self._agent_groups[idx]
            self._create_agent(context, agent_id, agent_data, self._agents_collection)
        self._agent_index = end
        progress = 25.0 + (self._agent_index / max(1, self._total_agents)) * 45.0
        context.scene.jupedsim_props.loading_progress = min(progress, 70.0)
        if self._agent_index >= self._total_agents:
            return True
        return False

    def _step_create_paths(self, context):
        """Create a small batch of agent path curves per tick."""
        if self._agent_groups is None:
            return True
        if self._path_index == 0:
            self._timed_start("create_paths")
        chunk_size = 10
        start = self._path_index
        end = min(self._total_agents, start + chunk_size)
        for idx in range(start, end):
            agent_id, agent_data = self._agent_groups[idx]
            self._create_agent_path(context, agent_id, agent_data, self._agents_collection)
        self._path_index = end
        progress = 70.0 + (self._path_index / max(1, self._total_agents)) * 25.0
        context.scene.jupedsim_props.loading_progress = min(progress, 95.0)
        if self._path_index >= self._total_agents:
            return True
        return False

    def _timed_start(self, name):
        """Start a named timing section."""
        self._timings[name] = -time.perf_counter()

    def _timed_end(self, name):
        """Stop a named timing section and log it."""
        if name in self._timings:
            self._timings[name] = time.perf_counter() + self._timings[name]
            print(f"[BlenderJPS] {name} took {self._timings[name]:.3f}s")

    def _log_timings(self):
        """Print the timing summary."""
        print("[BlenderJPS] Timing summary:")
        for key, value in self._timings.items():
            print(f"  - {key}: {value:.3f}s")

    def _print_worker_traceback(self):
        """Print worker thread traceback if available."""
        trace = getattr(self, "_worker_traceback", None)
        if trace:
            print(trace)

    def _clear_big_data_state(self):
        """Remove frame handlers and clear big data buffers."""
        if BIG_DATA_STATE["handler_installed"]:
            if _big_data_frame_handler in bpy.app.handlers.frame_change_pre:
                bpy.app.handlers.frame_change_pre.remove(_big_data_frame_handler)
        BIG_DATA_STATE["positions"] = None
        BIG_DATA_STATE["min_frame"] = 0
        BIG_DATA_STATE["max_frame"] = 0
        BIG_DATA_STATE["object_name"] = None
        BIG_DATA_STATE["handler_installed"] = False
    
    def _get_or_create_collection(self, name):
        """Get or create a collection with the given name."""
        if name in bpy.data.collections:
            collection = bpy.data.collections[name]
            # Clear existing objects
            for obj in list(collection.objects):
                bpy.data.objects.remove(obj, do_unlink=True)
        else:
            collection = bpy.data.collections.new(name)
            bpy.context.scene.collection.children.link(collection)
        return collection
    
    def _create_agent(self, context, agent_id, agent_data, collection):
        """Create an animated empty object for a single agent."""
        # Create an empty object for the agent (much faster than mesh)
        empty_obj = bpy.data.objects.new(f"Agent_{agent_id}", None)
        empty_obj.empty_display_type = 'SPHERE'
        empty_obj.empty_display_size = 0.5  # Radius 0.5 = diameter 1
        
        # Add to collection
        collection.objects.link(empty_obj)
        
        # Track the last frame this agent appears in
        last_agent_frame = None
        
        x_col = 'x' if 'x' in agent_data.columns else 'pos_x'
        y_col = 'y' if 'y' in agent_data.columns else 'pos_y'

        # Add keyframes for sampled frames only
        for _, row in agent_data.iterrows():
            frame = int(row['frame'])
            if frame not in self._sampled_frames:
                continue

            x = float(row[x_col])
            y = float(row[y_col])
            z = 0.5

            empty_obj.location = (x, y, z)
            empty_obj.keyframe_insert(data_path="location", frame=frame)

            if last_agent_frame is None or frame > last_agent_frame:
                last_agent_frame = frame
        
        if empty_obj.animation_data and empty_obj.animation_data.action:
            for fcurve in empty_obj.animation_data.action.fcurves:
                for keyframe in fcurve.keyframe_points:
                    keyframe.interpolation = 'LINEAR'
        
        if last_agent_frame is not None and last_agent_frame < self._max_frame:
            empty_obj.hide_viewport = False
            empty_obj.hide_render = False
            empty_obj.keyframe_insert(data_path="hide_viewport", frame=last_agent_frame)
            empty_obj.keyframe_insert(data_path="hide_render", frame=last_agent_frame)
            
            empty_obj.hide_viewport = True
            empty_obj.hide_render = True
            empty_obj.keyframe_insert(data_path="hide_viewport", frame=last_agent_frame + 1)
            empty_obj.keyframe_insert(data_path="hide_render", frame=last_agent_frame + 1)
            
            empty_obj.hide_viewport = False
            empty_obj.hide_render = False
    
    def _create_geometry(self, context, geometry, collection):
        """Create curves from the walkable area geometry."""
        # Get the shapely polygon from the walkable area
        polygon = geometry.polygon
        
        # Create curves for exterior boundary
        self._create_curve_from_coords(
            context,
            "Walkable_Area_Boundary",
            list(polygon.exterior.coords),
            collection,
            closed=True
        )
        
        # Create curves for any interior holes (obstacles)
        for i, interior in enumerate(polygon.interiors):
            self._create_curve_from_coords(
                context,
                f"Obstacle_{i}",
                list(interior.coords),
                collection,
                closed=True
            )
        
        self.report({'INFO'}, f"Created geometry with {1 + len(list(polygon.interiors))} boundary curves")
    
    def _create_curve_from_coords(self, context, name, coords, collection, closed=False):
        """Create a curve object from a list of coordinates."""
        # Create curve data
        curve_data = bpy.data.curves.new(name=name, type='CURVE')
        curve_data.dimensions = '3D'
        curve_data.resolution_u = 2
        
        # Create spline
        spline = curve_data.splines.new('POLY')
        spline.points.add(len(coords) - 1)  # One point already exists
        
        # Set point coordinates (x, y, z, w)
        for i, coord in enumerate(coords):
            x, y = coord[0], coord[1]
            z = 0.0  # Ground level for geometry
            spline.points[i].co = (x, y, z, 1.0)
        
        if closed:
            spline.use_cyclic_u = True
        
        # Create curve object
        curve_obj = bpy.data.objects.new(name, curve_data)
        
        # Add to collection
        collection.objects.link(curve_obj)
        
        # Add some visual thickness to the curve
        curve_data.bevel_depth = 0.05
        curve_data.bevel_resolution = 2
        
        return curve_obj
    
    def _create_agent_path(self, context, agent_id, agent_data, collection):
        """Create a curve representing the path of an agent."""
        # Get all coordinates for this agent's path
        coords = []
        x_col = 'x' if 'x' in agent_data.columns else 'pos_x'
        y_col = 'y' if 'y' in agent_data.columns else 'pos_y'
        for _, row in agent_data.iterrows():
            x = float(row[x_col])
            y = float(row[y_col])
            z = 0.0  # below the agent, at the height of the ground
            coords.append((x, y, z))
        
        if len(coords) < 2:
            return  # Need at least 2 points for a curve
        
        # Create curve data
        curve_data = bpy.data.curves.new(name=f"Path_Agent_{agent_id}", type='CURVE')
        curve_data.dimensions = '3D'
        curve_data.resolution_u = 2
        
        # Create spline
        spline = curve_data.splines.new('POLY')
        spline.points.add(len(coords) - 1)  # One point already exists
        
        # Set point coordinates (x, y, z, w)
        for i, coord in enumerate(coords):
            spline.points[i].co = (*coord, 1.0)
        
        # Path is not closed
        spline.use_cyclic_u = False
        
        # Create curve object
        curve_obj = bpy.data.objects.new(f"Path_Agent_{agent_id}", curve_data)
        
        # Add to collection
        collection.objects.link(curve_obj)
        
        # Add visual thickness to the path curve (thinner than geometry)
        curve_data.bevel_depth = 0.02
        curve_data.bevel_resolution = 2
        
        # Set material color (optional - can be customized)
        # For now, just make it visible but will be controlled by toggle
        
        return curve_obj
    
    def _create_big_data_points(self, context):
        """Create a single mesh driven by frame-change handler."""
        if not self._worker_data or not self._worker_data.get("frame_positions"):
            return
        frame_positions = self._worker_data["frame_positions"]
        agent_ids = self._worker_data["agent_ids"] or []
        if not agent_ids:
            return

        mesh = bpy.data.meshes.new("JuPedSim_Particles")
        mesh.vertices.add(len(agent_ids))
        mesh.vertices.foreach_set("co", frame_positions[0])
        mesh.update()

        obj = bpy.data.objects.new("JuPedSim_Particles", mesh)
        obj.display_type = 'WIRE'
        obj.show_in_front = True
        self._agents_collection.objects.link(obj)

        BIG_DATA_STATE["positions"] = frame_positions
        BIG_DATA_STATE["min_frame"] = self._min_frame
        BIG_DATA_STATE["max_frame"] = self._max_frame
        BIG_DATA_STATE["object_name"] = obj.name
        if not BIG_DATA_STATE["handler_installed"]:
            bpy.app.handlers.frame_change_pre.append(_big_data_frame_handler)
            BIG_DATA_STATE["handler_installed"] = True
    
    def _update_path_visibility(self, collection, visible):
        """Update visibility of all agent path curves in the collection."""
        for obj in collection.objects:
            if obj.name.startswith("Path_Agent_"):
                obj.hide_viewport = not visible
                obj.hide_render = not visible


classes = [
    JUPEDSIM_OT_select_file,
    JUPEDSIM_OT_load_simulation,
]


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)

