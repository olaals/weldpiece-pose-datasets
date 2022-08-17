import graphslam
from graphslam.graph import Graph
from graphslam.pose.se3 import PoseSE3
from graphslam.vertex import Vertex
from graphslam.edge.edge_odometry import EdgeOdometry
from collections import OrderedDict
import itertools
import cv2
from cv2 import aruco as aruco
import numpy as np
from gui_utils import read_rgb, get_image_paths_from_dir
import matplotlib.pyplot as plt
import os
import spatialmath as sm
from scipy.spatial.transform import Rotation as spR
from utils_aruco import set_axes_equal, plot_3d_graph, TwoWayDict
import pickle
from renderer import render_scene
import numpy as np
from debug import nice_print_dict
import g2o

class PoseGraphOptimization(g2o.SparseOptimizer):
    def __init__(self):
        super().__init__()
        solver = g2o.BlockSolverSE3(g2o.LinearSolverCholmodSE3())
        solver = g2o.OptimizationAlgorithmLevenberg(solver)
        super().set_algorithm(solver)

    def optimize(self, max_iterations=20):
        super().initialize_optimization()
        super().optimize(max_iterations)

    def add_vertex(self, id, pose, fixed=False):
        v_se3 = g2o.VertexSE3()
        v_se3.set_id(id)
        v_se3.set_estimate(pose)
        v_se3.set_fixed(fixed)
        super().add_vertex(v_se3)

    def add_edge(self, vertices, measurement,
            information=np.identity(6),
            robust_kernel=None):

        edge = g2o.EdgeSE3()
        for i, v in enumerate(vertices):
            if isinstance(v, int):
                v = self.vertex(v)
            edge.set_vertex(i, v)

        edge.set_measurement(measurement)  # relative pose
        edge.set_information(information)
        if robust_kernel is not None:
            edge.set_robust_kernel(robust_kernel)
        super().add_edge(edge)

    def get_pose(self, id):
        return self.vertex(id).estimate()

def create_board(squares_x, squares_y, cb_sq_width, aruco_sq_width, aruco_dict_str, start_id):
    aruco_dict = aruco.Dictionary_get(getattr(aruco, aruco_dict_str))
    aruco_dict.bytesList=aruco_dict.bytesList[start_id:,:,:]
    board = aruco.CharucoBoard_create(squares_x,squares_y,cb_sq_width,aruco_sq_width,aruco_dict)
    return board, aruco_dict


def get_aruko_poses(img, K, aruco_dict_str, aruko_sq_size):
    board, aruco_dict = create_board(4,3,49.9*1e-3,32.44*1e-3, aruco_dict_str, 6)
    ar_params = aruco.DetectorParameters_create()
    ar_params.cornerRefinementMethod = aruco.CORNER_REFINE_APRILTAG
    dist = np.zeros(5)
    aruko_poses = []
    (marker_corners, marker_ids, rejected) = aruco.detectMarkers(img, aruco_dict, parameters=ar_params)
    if marker_ids is not None and len(marker_ids) > 0:
        num, char_corners, char_ids = aruco.interpolateCornersCharuco(marker_corners, marker_ids, img, board, cameraMatrix=K, distCoeffs=np.zeros(5))
        if(char_ids is not None and len(char_ids)>0):
            valid, rvec,tvec = aruco.estimatePoseCharucoBoard(char_corners, char_ids, board, K, dist, np.empty(1), np.empty(1))
            if valid:
                T_CA = np.identity(4)
                R, _ = cv2.Rodrigues(rvec)
                T_CA[:3,:3] = R
                T_CA[:3,3] = tvec.flatten()
                aruko_poses.append(("[0]", T_CA))
    return aruko_poses



def draw_markers(img, K, aruco_dict_str):
    aruco_dict = aruco.Dictionary_get(getattr(aruco, aruco_dict_str))
    ar_params = aruco.DetectorParameters_create()
    ar_params.cornerRefinementMethod = aruco.CORNER_REFINE_APRILTAG
    (marker_corners, marker_ids, rejected) = aruco.detectMarkers(img, aruco_dict, parameters=ar_params)
    dist = np.zeros(5)
    if marker_ids is not None and len(marker_ids) > 0:
        img = aruco.drawDetectedMarkers(img, marker_corners, marker_ids)
        for single_marker_corner, marker_id in zip(marker_corners, marker_ids):
            rvec,tvec,obj_p_corners = aruco.estimatePoseSingleMarkers(single_marker_corner, 66.0*1e-3, K, dist)
            cv2.drawFrameAxes(img, K, dist, rvec,tvec,0.05)
    return img


def add_model_to_poses(img_basename, pose_dict, poses):
    print("ADD Model to poses")
    nice_print_dict(pose_dict)
    print(pose_dict[img_basename])
    if "T_CO" in pose_dict[img_basename]:
        print(img_basename)
        poses.append(["model", pose_dict[img_basename]["T_CO"]])
    return poses



def contains_idx_in_graph(graph, ar_poses):
    for (marker_idx, T_CA) in ar_poses:
        if str(marker_idx) in graph:
            return str(marker_idx), T_CA
    return None, None

def init_aruco_graph(graph, img_paths, K, aruco_dict_str, aruko_sq_size):

    graph["[0]"] = np.identity(4)

    graph_changed = True
    while graph_changed:
        graph_changed = False
        for img_path in img_paths:
            print("init aruco graph", img_path)
            img = read_rgb(img_path)
            ar_poses = get_aruko_poses(img, K, aruco_dict_str, aruko_sq_size)
            print("init graph ar poses", ar_poses)
            contained_idx, T_CA = contains_idx_in_graph(graph, ar_poses)
            print("contained idx", contained_idx, "T_CA", T_CA)
            if contained_idx:
                for (marker_idx, T_CA_c) in ar_poses:
                    if not str(marker_idx) in graph:
                        print("contained idx", contained_idx, "marker_idx", marker_idx)
                        graph[str(marker_idx)] = graph[str(contained_idx)]@np.linalg.inv(T_CA)@T_CA_c
                        graph_changed=True
    return graph






def init_aruco_pose_graph(img_paths, K, aruco_dict_str, aruko_sq_size, pose_dict):
    outgraph = {}
    outgraph = init_aruco_graph(outgraph, img_paths, K, aruco_dict_str, aruko_sq_size)
    for img_path in img_paths:
        img = read_rgb(img_path)
        poses = get_aruko_poses(img, K, aruco_dict_str, aruko_sq_size)
        img_basename = os.path.basename(img_path)
        contained_idx, T_WA = contains_idx_in_graph(outgraph, poses)
        outgraph["cam_"+img_basename] = outgraph[str(contained_idx)]@np.linalg.inv(T_WA)

        #for (marker_idx, T_CA) in poses:
            #outgraph[str(marker_idx)] = np.identity(4)

    for img_basename in pose_dict:
        if "T_CO" in pose_dict[img_basename]:
            T_CO = pose_dict[img_basename]["T_CO"]
            outgraph["model"] = outgraph["cam_"+img_basename]@T_CO
            break
    return OrderedDict(sorted(outgraph.items()))



def create_image_pose_dict(img_paths, K, aruco_dict_str, aruko_sq_size):
    aruco_dict = aruco.Dictionary_get(getattr(aruco, aruco_dict_str))
    img_paths.sort()
    img_pose_dict = {}
    for img_path in img_paths:
        img = read_rgb(img_path)
        poses = get_aruko_poses(img, K, aruco_dict_str, aruko_sq_size)
        print("create img pose dict. Poses:", poses)
        img_basename = os.path.basename(img_path)
        print(img_basename)
        print("create img pose dict. img basename:", img_basename)
        img_pose_dict[img_basename] = poses
    return OrderedDict(sorted(img_pose_dict.items()))

def T_to_PoseSE3(T):
    R = T[:3,:3] 
    t = T[:3,3]
    q = spR.from_matrix(R).as_quat()
    T_PoseSE3 = PoseSE3(t, q)
    return T_PoseSE3


def create_graphslam_vertex(idx, T_W):
    T_W_gs = T_to_PoseSE3(T_W)
    return Vertex(idx, T_W_gs)

def create_graphslam_edge(idx_k, idx_l, T_kl, info_mat):
    T_kl_gs = T_to_PoseSE3(T_kl)
    edge = EdgeOdometry([int(idx_k), int(idx_l)], info_mat, T_kl_gs)
    return edge

def init_vertices_graphslam(init_graph, g2o_graph):
    id_vert_dict = TwoWayDict()
    for i,marker_idx in enumerate(init_graph):
        fixed = (i == 0)
        id_vert_dict[marker_idx] = str(i)
        T_WA = init_graph[marker_idx]
        g2o_graph.add_vertex(i, g2o.Isometry3d(T_WA[:3,:3], T_WA[:3,3]), fixed)
    return id_vert_dict



def create_edges_graphslam(image_pose_dict, id_vert_dict, pose_dict, g2o_graph):
    for key in id_vert_dict:
        print(key, id_vert_dict[key])

    print("#¤%&/()      CREATE EDGES GRAPHSLAM")
    nice_print_dict(image_pose_dict)

    aruko_info_mat = np.identity(6).astype(np.float32)*1.0
    model_info_mat = np.identity(6).astype(np.float32)*1.0
    #inf_mat_model = np.identity(6).astype(np.float32)
    for image_basename in image_pose_dict:
        image_poses = image_pose_dict[image_basename]
        print(image_pose_dict[image_basename])
        image_poses = add_model_to_poses(image_basename, pose_dict, image_poses)
        for (marker_idx, T_CA) in image_poses:
            cam_label = "cam_"+image_basename
            idx_cam = int(id_vert_dict[cam_label])
            marker_idx = int(id_vert_dict[str(marker_idx)])
            print("cam_label", cam_label, "mark idx", marker_idx)
            if marker_idx == 'model':
                info_mat = model_info_mat
            else:
                info_mat = aruko_info_mat
            g2o_graph.add_edge([idx_cam, marker_idx], g2o.Isometry3d(T_CA[:3,:3], T_CA[:3,3]), info_mat, g2o.RobustKernelHuber(np.sqrt(5.991)))



    






def optimize_aruko_graph(init_graph, image_pose_dict, pose_dict):
    g2o_graph = PoseGraphOptimization()
    id_vert_dict = init_vertices_graphslam(init_graph, g2o_graph)
    print("ID vert dict")
    nice_print_dict(id_vert_dict)
    create_edges_graphslam(image_pose_dict, id_vert_dict, pose_dict, g2o_graph)
    g2o_graph.optimize(max_iterations=100)
    print(g2o_graph.get_pose(0).matrix())
    print(g2o_graph.get_pose(1).matrix())
    num_poses = len(id_vert_dict)
    print(num_poses)
    out_graph = {}
    for idx in range(num_poses):
        str_repr = id_vert_dict[str(idx)]
        out_graph[str_repr]= np.array(g2o_graph.get_pose(idx).matrix())
    return out_graph


def get_T_CO(graph, img_basename):
    T_WC = graph["cam_"+img_basename]
    T_WO = graph["model"]
    T_CO = np.linalg.inv(T_WC)@T_WO
    return T_CO

def aruko_optimize_handler(img_paths, K, aruco_dict_str, aruco_sq_size, pose_dict):

    out_dict = {}
    init_graph = init_aruco_pose_graph(img_paths, K, aruco_dict_str, aruco_sq_size, pose_dict)
    img_pose_dict = create_image_pose_dict(img_paths, K, aruco_dict_str, aruco_sq_size)
    out_graph = optimize_aruko_graph(init_graph, img_pose_dict, pose_dict)

    #fig = plt.figure()
    #ax1 = fig.add_subplot(1,2,1,projection='3d')
    #ax2 = fig.add_subplot(1,2,2,projection='3d')
    #plot_3d_graph(ax1, init_graph)
    #plot_3d_graph(ax2, out_graph)
    #set_axes_equal(ax1)
    #set_axes_equal(ax2)
    ## EXIT ##
    #plt.show()
    for img_path in img_paths:
        img_basename = os.path.basename(img_path)
        out_dict[img_basename] = {}
        out_dict[img_basename]["T_CO_opt"] = get_T_CO(out_graph, img_basename)
    return out_dict
    



if __name__ == '__main__':
    with open('pose_dict.pkl', 'rb') as handle:
        pose_dict = pickle.load(handle)
    K = np.array([[1166.3, .0, 509],[0, 1166.0, 546.0],[0,0,1.0]])
    print(type(K[0,0]))
    K_load = np.load("K.npy")
    print(type(K_load[0,0]))
    print("K load")
    print(np.load("K.npy"))
    
    #print("K_old", K_old)
    print("K", K)
    K = K.astype(np.float64)
    img_dir = "/home/ola/projects/weldpiece-pose-datasets/ds-projects/office-corner-brio-charuco/captures"
    img_paths = get_image_paths_from_dir(img_dir)
    aruco_dict_str = "DICT_APRILTAG_16H5"
    aruco_sq_size = 66.0*1e-3

    opt_dict = aruko_optimize_handler(img_paths, K, aruco_dict_str, aruco_sq_size, pose_dict)

    obj_path = "/home/ola/projects/weldpiece-pose-datasets/3d-models/corner.ply"

    nice_print_dict(opt_dict)

    for key in opt_dict:
        T_CO = opt_dict[key]["T_CO_opt"]
        print("T_CO render")
        print(T_CO)
        img, dep = render_scene(obj_path, T_CO, K, (1080,1080))
        plt.imshow(img)
        plt.show()


