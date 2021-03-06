import numpy as np
import cv2
from matplotlib import pyplot as plt
import pickle
import time
from pathlib import Path
import codecs
import math
import multiprocessing as mp
import copy
from sklearn.cluster import KMeans
import os
from utility import *
# from hwang import Decoder
# from storehouse import StorageConfig, StorageBackend, RandomReadFile

def detect_single(video_meta, face_list, com_list=None):
    fps = video_meta['fps']
    video_length = video_meta['video_length']

    single_person = []
    cnt_face = 0
    for shot in face_list:
        cnt_face += len(shot['faces'])
        if len(shot['faces']) >= 1 and len(shot['faces']) <= 3:
            for i, face in enumerate(shot['faces']):
                face_area = (face[0]['bbox_y2'] - face[0]['bbox_y1']) * (face[0]['bbox_x2'] - face[0]['bbox_x1'])
                if face_area < 0.02 and face[0]['bbox_y1'] > 0.5:
                    continue
                body_bound = 1.0
                ## detect other face below current face as body bound
                for j, other_face in enumerate(shot['faces']):
                    if i != j and face[0]['bbox_y2'] < other_face[0]['bbox_y1']:
                        center = (face[0]['bbox_x1'] + face[0]['bbox_x2']) / 2
                        crop_x1 = int(center - face[0]['bbox_x2'] + face[0]['bbox_x1'])
                        crop_x2 = int(center + face[0]['bbox_x2'] - face[0]['bbox_x1'])
                        if other_face[0]['bbox_x1'] < crop_x2 or other_face[0]['bbox_x2'] > crop_x1:
                            body_bound = other_face[0]['bbox_y1'] 
                
                single_person.append({'fid': shot['face_frame'], 'shot': (shot['min_frame'], shot['max_frame']),
                    'bbox': face[0], 'feature': np.array(face[1]), 'face_per_shot': len(shot['faces']), 'body_bound': body_bound})
#     print(len(single_person))

    # remove face in commercial
    if not com_list is None:
        single_person_raw = single_person
        single_person = []
        for p in single_person_raw:
            t = get_time_from_fid(p['fid'], fps)
            is_in_com = False
            for com in com_list:
                if is_time_in_window(t, (com[0][1], com[1][1])):
                    is_in_com = True
                    break
            if not is_in_com:
                single_person.append(p)
#     print(cnt_face, len(single_person))
    return single_person
    
def detect_anchor(video_name, video_meta, single_person, com_list=None, detail=True):
    fps = video_meta['fps']
    video_length = video_meta['video_length']
    
    FACE_SIM_THRESH = 1.0

    ## detect anchor by Kmeans
    NUM_CLUSTER = 10
    N = len(single_person)
    if N < NUM_CLUSTER:
        return None
    features = np.zeros((N, 128))
    for i in range(len(single_person)):
        features[i] = single_person[i]['feature']
    kmeans = KMeans(n_clusters=NUM_CLUSTER).fit(features)
    ## find the real cluster center in the dataset
    cluster_center = []
    for i in range(NUM_CLUSTER):
        c = kmeans.cluster_centers_[i]
        sim = []
        for p in single_person:
            dist = np.linalg.norm(p['feature'] - c)
            sim.append(dist)
        cluster_center.append(single_person[np.argmin(sim)])

    ## find same face for each cluster center
#     FACE_SIM_SIDE_THRESH = 1.25
#     cluster = []
#     cluster_side = []
#     for i in range(NUM_CLUSTER):
#         group = []
#         side = []
#         center = cluster_center[i]
#         for p in single_person:
#             sim = np.linalg.norm(p['feature'] - center['feature'])
#             if sim < FACE_SIM_THRESH:
#                 group.append(copy.deepcopy(p))
#             elif sim < FACE_SIM_SIDE_THRESH:
#                 side.append(copy.deepcopy(p))
#         cluster.append(group)
#         cluster_side.append(side)
        
    ## find same face for each cluster center (exclusive)
    cluster = [[] for i in range(NUM_CLUSTER)]
    cluster_side = [[] for i in range(NUM_CLUSTER)]
    for p in single_person:
        sims = []
        for i in range(NUM_CLUSTER):
            center = cluster_center[i]
            sim = np.linalg.norm(p['feature'] - center['feature'])
            sims.append(sim)
        sim_min_c = np.argmin(sims)
        if sims[sim_min_c] < FACE_SIM_THRESH:
            cluster[sim_min_c].append(copy.deepcopy(p))
    
    ## merge similar clusters
    MERGE_THRESH = 0.9
    while True:
        merge_pair = None
        for i, c1 in enumerate(cluster_center):
            if not merge_pair is None:
                break
            for j in range(i+1, len(cluster_center)):
                c2 = cluster_center[j]
                sim = np.linalg.norm(c1['feature']- c2['feature'])
                if sim < MERGE_THRESH:
                    if detail:
                        print("Similarity between center %d and center %d = %f" % (i, j, sim))
                    if len(cluster[i]) < len(cluster[j]):
                        merge_pair = (i, j)
                    else:
                        merge_pair = (i, j)
                    break
        if not merge_pair is None:
            center = cluster_center[merge_pair[1]]
            for p in cluster[merge_pair[0]]:
                sim = np.linalg.norm(p['feature']- center['feature'])
                if sim < FACE_SIM_THRESH:
                    cluster[merge_pair[1]].append(p)
            del cluster[merge_pair[0]]
            del cluster_center[merge_pair[0]]
            del cluster_side[merge_pair[0]]
        else:
            break
        
    ## remove noise face by calculating average similiraty
    FACE_SIM_THRESH_CLUSTER = 0.95
    for i in range(len(cluster)):
        if len(cluster[i]) <= 1:
            continue
        sim = []
        for p1 in cluster[i]:
            dist = 0
            for p2 in cluster[i]:
                if p1['fid'] != p2['fid']:
                    dist += np.linalg.norm(p1['feature'] - p2['feature'])
            sim.append(dist / (len(cluster[i])-1))
        top_id = np.argsort(sim)
        new_group_idx = []
        ## remove noise at the end
        for idx in top_id:
            if sim[idx] > FACE_SIM_THRESH_CLUSTER:
                ## collect side
                cluster_side[i].append(cluster[i][idx])
            else:
                new_group_idx.append(idx)
#                 cluster[i][idx]['sim'] = sim[idx]
        new_group_idx.sort()
        new_group = []
        cnt_large = 0
        for idx in new_group_idx:
            p = cluster[i][idx]
            p['sim'] = np.linalg.norm(p['feature'] - cluster_center[i]['feature'])
            if p['sim'] > 0.5:
                cnt_large += 1
            new_group.append(p)
        origin_faces = len(cluster[i])
        ## remove cluster with no center
        if cnt_large == len(new_group):
            cluster[i] = []
        else:
            cluster[i] = new_group
        if detail:
            print("Cluster %d: %d faces -> %d faces" % (i, origin_faces, len(cluster[i])))
            
    ## calculate the cluster coverage, duration, first appear time
    BIN_WIDTH = 150
    coverage = []
    duration = []
    firstAppear = []
    shotGap = []
    for i in range(len(cluster)):
        bins = np.zeros(int(video_length / BIN_WIDTH)+1)
        dua = 0
        gaps = []
        for j, p in enumerate(cluster[i]):
            t = get_second_from_fid(p['fid'], fps)
            bins[int(t / BIN_WIDTH)] += 1
            dua += (p['shot'][1] - p['shot'][0]) / fps / video_length
            if j != 0:
                gaps.append((p['fid'] - cluster[i][j-1]['fid']) / fps)
        cov = 1. * np.count_nonzero(bins) / (bins.shape[0])
        coverage.append(cov)
        duration.append(dua)
        gaps.sort()
        if len(cluster[i]) > 0:
            firstAppear.append(get_second_from_fid(cluster[i][0]['shot'][0], fps))
            shotGap.append(gaps)
        else:
            firstAppear.append(0)
            shotGap.append([0])
#         l = (get_second_from_fid(c[-1]['fid'], fps) - get_second_from_fid(c[0]['fid'], fps)) / video_length
        
    ## find the cluster with the most broad distribution
    anchor_group = []
    anchor_side = []
    anchor_center = []
    cvg_sort = np.argsort(coverage)[::-1]
    last_idx = cvg_sort[0]
    cvg_max = coverage[cvg_sort[0]]
    for i, idx in enumerate(cvg_sort[1:]):
        if detail:
            print("Cluster %d coverage, duration, shotGap: %.3f, %.3f, %.3f" % (i, coverage[last_idx], duration[last_idx], shotGap[last_idx][int(len(shotGap[last_idx])*0.75)]))
        anchor_group.append(cluster[last_idx])
        anchor_side.append(cluster_side[last_idx])
        anchor_center.append(cluster_center[last_idx])
        if coverage[idx] / cvg_max > 0.8:#0.8
            last_idx = idx
        else:
            break
            
    if len(anchor_group) == 0:
        return None
    elif len(anchor_group) > 2:
        first_min_idx = np.argmin(firstAppear)
        anchor_group = []
        anchor_side = []
        anchor_center = []
        anchor_group.append(cluster[first_min_idx])
        anchor_side.append(cluster_side[first_min_idx])
        anchor_center.append(cluster_center[first_min_idx])
        
    
    ## if large cluster exist, remove small cluster
#     anchor_group_raw = anchor_group
#     anchor_side_raw = anchor_side
#     anchor_center_raw = anchor_center
#     anchor_group = []
#     anchor_side = []
#     anchor_center = []
#     for i, anchor_person in enumerate(anchor_group_raw):
#         if anchor_cov_dua[i][0] * 2 > max_coverage and anchor_cov_dua[i][1] * 2 > max_duration:
#             anchor_group.append(anchor_group_raw[i])
#             anchor_side.append(anchor_side_raw[i])
#             anchor_center.append(anchor_center_raw[i])
#     if len(anchor_group) < len(anchor_group_raw):
#         print(video_name + '++++++++++++++++++++++++++++++++++++++++++')
    
    ## reorder
    cnt_face = 0
    for i, anchor_person in enumerate(anchor_group):
        for j, p in enumerate(anchor_group[i]):
            cnt_face += 1
            anchor_group[i][j]['fake'] = False
            sim = np.linalg.norm(anchor_center[i]['feature'] - p['feature'])
            anchor_group[i][j]['sim'] = sim
        
#         for j, p in enumerate(anchor_side[i]):
#             anchor_side[i][j]['fake'] = True    
#             sim = np.linalg.norm(anchor_center[i]['feature'] - p['feature'])
#             anchor_side[i][j]['sim'] = sim
            
#         for j in range(len(anchor_group[i])):
#             for k in range(j+1, len(anchor_group[i])):
#                 if anchor_group[i][j]['sim'] > anchor_group[i][k]['sim']:
#                     tmp = anchor_group[i][j]
#                     anchor_group[i][j] = anchor_group[i][k]
#                     anchor_group[i][k] = tmp
        
#         for j in range(len(anchor_side[i])):
#             for k in range(j+1, len(anchor_side[i])):
#                 if anchor_side[i][j]['sim'] > anchor_side[i][k]['sim']:
#                     tmp = anchor_side[i][j]
#                     anchor_side[i][j] = anchor_side[i][k]
#                     anchor_side[i][k] = tmp

#         anchor_group[i].extend(anchor_side[i])
    print('Labeled face: %d, Total face: %d' % (cnt_face, len(single_person)))                
    return anchor_group

def get_middle_anchor(anchor_group, detail=True):
    ## use the x of the center of the bbox to define the center
    for i, anchor_person in enumerate(anchor_group):
        face_pos = []
        for j, p in enumerate(anchor_person):
            if p['fake']:
                continue
            cx = (p['bbox']['bbox_x1'] + p['bbox']['bbox_x2']) / 2
            cy = (p['bbox']['bbox_y1'] + p['bbox']['bbox_y2']) / 2
            area = (p['bbox']['bbox_y2'] - p['bbox']['bbox_y1']) * ((p['bbox']['bbox_x2'] - p['bbox']['bbox_x1']))
            face_pos.append(np.array([cx, cy, area]))
        NUM_CLUSTER = 8
        if len(face_pos) < NUM_CLUSTER:
            NUM_CLUSTER = len(face_pos)
          
        kmeans = KMeans(n_clusters=NUM_CLUSTER).fit(np.array(face_pos))
        ## collect the cluster
        cluster_center = []
        cluster = []
        for k in range(NUM_CLUSTER):
            cluster_center.append(kmeans.cluster_centers_[k])
            cluster.append([])
        for j, c in enumerate(kmeans.labels_):
            cluster[c].append(j)
        ## merge similar clusters
        POS_THRESH = 0.04
        AREA_THRESH = 0.01
        while True:
            del_key = -1
            for i, c1 in enumerate(cluster_center):
                if del_key != -1:
                    break
                for j in range(i+1, len(cluster_center)):
                    c2 = cluster_center[j]
                    dist = np.linalg.norm(c1[:2]- c2[:2])
                    if dist < POS_THRESH and np.fabs(c1[2] - c2[2]) < AREA_THRESH:
                        if detail:
                            print("Similarity between center %d and center %d = %f" % (i, j, dist))
                        if len(cluster[i]) < len(cluster[j]):
                            cluster[j].extend(cluster[i])
                            del_key = i
                        else:
                            cluster[i].extend(cluster[j])
                            del_key = j
                        break
            if del_key != -1:
                del cluster[del_key]
                del cluster_center[del_key]
                if detail:
                    print("delete", del_key)
            else:
                break
        
#         face_area = []
#         for k, c in enumerate(cluster):
#             area = 0.0
#             for idx in c:
#                 area += face_pos[idx][2]
#             area /= len(c)
#             face_area.append(area)
#         print(face_area)
        
        AREA_THRESH = 0.06
#         for k, c in enumerate(cluster):
#             if face_area[k] > AREA_THRESH:
#                 for idx in c:
#                     anchor_person[idx]['type'] = 'large'
#             else:
#                 for idx in c:
#                     anchor_person[idx]['type'] = 'small'
        for k, c in enumerate(cluster):
            for idx in c:
                anchor_person[idx]['type'] = k
                
        ## check every unfake person face has type
#         for p in anchor_person:
#             if not p['fake'] and not 'type' in p:
#                 print("++++++++++++++++++++++++++++++++++++++++++++++++++++++++")

def plot_cluster(video_name, video_meta, anchor_group):
    fps = video_meta['fps']
    video_path = '../data/videos/' + video_name + '.mp4'
    cap = cv2.VideoCapture(video_path)
    ret, frame = cap.read()
    if not ret:
        return 
    fid = 1
    H, W, C = frame.shape
    anchor_true = []
    anchor_false = []
    for anchor_person in anchor_group:
        N = len(anchor_person)
        cnt = 0
        for p in anchor_person:
            if p['fake'] == False:
                cnt += 1 
        anchor_true.append(np.zeros((cnt, H, W, C)))
        if N != cnt:
            anchor_false.append(np.zeros((N-cnt, H, W, C)))
    while(True):
        ret, frame = cap.read()
        if not ret:
            break

        for i, anchor_person in enumerate(anchor_group):
            for j, face in enumerate(anchor_person):
                if face['fid'] == fid:
                    img = copy.deepcopy(frame) 
                    x1 = int(face['bbox']['bbox_x1'] * W)
                    y1 = int(face['bbox']['bbox_y1'] * H)
                    x2 = int(face['bbox']['bbox_x2'] * W)
                    y2 = int(face['bbox']['bbox_y2'] * H)
                    
                    time = get_time_from_fid(fid, fps)
                    text = str(fid) + '|' + str(time) + '|' + '{0:.3f}'.format(face['sim'])
                    cv2.rectangle(img, (0, 0), (320, 30), color=(255,255,255), thickness=-1)
                    cv2.putText(img, text, (0,25), cv2.FONT_HERSHEY_SIMPLEX, fontScale=0.7, color=(0,0,0), thickness=2)
                    
                    if face['fake'] == False:
                        color = [(0, 0, 255), (255, 0, 0), (0, 255, 0), (255, 0, 255), (255, 255, 0), (0, 255, 255), (128, 128, 255), (128, 255, 128)]
#                         cv2.rectangle(img, (x1, y1), (x2, y2), color=color[face['type']], thickness=3)
                        cv2.rectangle(img, (x1, y1), (x2, y2), color=color[0], thickness=3)
                        if face['sim'] == 0:
                            filename = '../tmp/anchor_single/' + video_name + '_' + str(i) + '.jpg'
                            cv2.imwrite(filename, img)
                        anchor_true[i][j] = img
                    else:
                        cv2.rectangle(img, (x1, y1), (x2, y2), color=(255,255,255), thickness=3)
                        anchor_false[i][j - len(anchor_true[i])] = img
        fid += 1
    cap.release()

    for i in range(len(anchor_true)):
        grid = view_grid(anchor_true[i], 5)
        H, W, C = grid.shape
        filename = '../tmp/anchor/' + video_name + '_' + str(i) + '.jpg'
        grid_small = cv2.resize(grid, (1920, int(H/W*1920)))
        cv2.imwrite(filename, grid_small)
#     for i in range(len(anchor_false)):
#         grid = view_grid(anchor_false[i], 5)
#         H, W, C = grid.shape
#         filename = '../tmp/anchor/' + video_name + '_' + str(i) + '_fake.jpg'
#         grid_small = cv2.resize(grid, (1920, int(H/W*1920)))
#         cv2.imwrite(filename, grid_small) 
    
def plot_distribution(video_meta, anchor_group, com_list=None):
    fps = video_meta['fps']    
    video_length = video_meta['video_length']
    fig = plt.figure()
    fig.set_size_inches(14, 4)
    color = ['b', 'g', 'y', 'c', 'k']
    for i in range(len(anchor_group)):
        anchor_person = anchor_group[i]
        for p in anchor_person:
            if p['fake'] == False:
                t = get_second_from_fid(p['fid'], fps)
                plt.plot([t, t], [i+0.5, i+1.5], color[i%5], linewidth=1.0)
        if not com_list is None:        
            for com in com_list:
                plt.plot([get_second(com[0][1]), get_second(com[1][1])], [i+1, i+1], 'r', linewidth=4.0)

    plt.ylim([0, len(anchor_group)+1])
    plt.xlim([0, video_length])
    plt.xlabel('video time (s)')
    cur_axes = plt.gca()
    cur_axes.axes.get_yaxis().set_visible(False)
    plt.show()
    
def solve_single_video(video_name, video_meta, face_list, com_list, plot_d=False, plot_c=False, detail=True):
    single_person = detect_single(video_meta, face_list, com_list)
    anchor_group = detect_anchor(video_name, video_meta, single_person, com_list, detail)
    
    if anchor_group is None:
        out = open('../log/detect_anchor.txt', 'a')
        out.write(video_name + '\n')
        out.close()
        return None
#     get_middle_anchor(anchor_group, detail)
   
    if plot_c:
        plot_cluster(video_name, video_meta, anchor_group)
    if plot_d:
        plot_distribution(video_meta, anchor_group, com_list)
    return anchor_group

def build_people_dict(anchor_dict):
    FACE_SIM_THRESH = 0.5
    people_dict = {}
    for video, anchor_group in anchor_dict.items():
        date, station, show = get_detail_from_video_name(video)
        if not show in people_dict:
            people_dict[show] = []
        for anchor_person in anchor_group:
            ## find center
            for p in anchor_person:
                if p['sim'] == 0:
                    anchor = p
                    break
            ## remove repeated person 
            non_repeat = True
            for p in people_dict[show]:
                dist = np.linalg.norm(p['feature'] - anchor['feature'])
                if dist < FACE_SIM_THRESH:
                    non_repeat = False
                    break
            if non_repeat:        
                people_dict[show].append(copy.deepcopy(anchor))
                
#     pickle.dump(people_dict, open('../data/people_dict.pkl', 'wb'))
    return people_dict

def clean_anchor_dict(anchor_dict, people_dict, repeat_cnt=None):
    FACE_SIM_THRESH = 0.9
    OTHER_SHOW_THRESH = 85
    anchor_dict_new = {}
    for video, anchor_group in anchor_dict.items():
        date, station, show = get_detail_from_video_name(video)
        anchor_group_new = []
        for anchor_person in anchor_group:
            ## find cluster center
            for p in anchor_person:
                if p['sim'] == 0:
                    anchor = p
                    break
            ## find this anchor in other shows
            cnt = 0
            for show_check, people_list in people_dict.items():
                if show != show_check:
                    for p in people_list:
                        dist = np.linalg.norm(p['feature'] - anchor['feature'])
                        if dist < FACE_SIM_THRESH:
                            cnt += 1
#                             print(show_check)
                            break
            if cnt < OTHER_SHOW_THRESH:
                anchor_group_new.append(anchor_person)
#             repeat_cnt.append(cnt)
            else:
                print(video, anchor['fid'], anchor['bbox'], cnt)
                url = 'http://104.198.10.97/frameserver/fetch?path=tvnews%2Fvideos%2F' + video + '.mp4&frame=' + str(anchor['fid'])
                print(url)
                print('')

        anchor_dict_new[video] = anchor_group_new
    return anchor_dict_new

def test_video_list(video_list_path):
    face_dict = pickle.load(open('../data/face_dict.pkl', 'rb'))
    com_dict = pickle.load(open('../data/commercial_dict.pkl', 'rb'))
    meta_dict = pickle.load(open('../data/video_meta_dict.pkl', 'rb'))
    for line in open(video_list_path):
        if len(line) < 5: 
            continue
        video_name = line[:-1]
        print(video_name)
        solve_single_video(video_name, meta_dict[video_name], face_dict[video_name], com_dict[video_name], True, False)

def test_single_video(video_name, plot_distribution=True, plot_d=False, plot_c=True):
    face_dict = pickle.load(open('../data/face_dict.pkl', 'rb'))
    com_dict = pickle.load(open('../data/commercial_dict.pkl', 'rb'))
    meta_dict = pickle.load(open('../data/video_meta_dict.pkl', 'rb'))
    solve_single_video(video_name, meta_dict[video_name], face_dict[video_name], com_dict[video_name], plot_d, plot_c)
    
def detect_anchor_t(video_list, anchor_dict_path, plot_c, thread_id):
    print("Thread %d start computing..." % (thread_id))
    meta_dict = pickle.load(open('../data/video_meta_dict.pkl', 'rb'))

    if not video_list is None:
        face_dict = pickle.load(open('../data/face_dict.pkl', 'rb'))
        com_dict = pickle.load(open('../data/commercial_dict.pkl', 'rb'))
        anchor_dict = {}
    else:
        dict_path = '../data/face_dict/face_dict_' + str(thread_id+32) + '.pkl'
        face_dict = pickle.load(open(dict_path, 'rb'))
#         anchor_dict_name = pickle.load(open('../data/anchor_dict_name.pkl', 'rb'))
        anchor_dict_name = {}
        com_dict = pickle.load(open('../data/commercial_dict.pkl', 'rb'))
        anchor_dict = {}
        video_list = [video for video in sorted(face_dict) if not video in anchor_dict_name]
    
    for i in range(len(video_list)):
        video_name = video_list[i]
        print("Thread %d start %dth video: %s" % (thread_id, i, video_name))
        if video_name in com_dict:
            com_list = com_dict[video_name]
        else:
            com_list = None
        if video_name in meta_dict:
            anchor_group = solve_single_video(video_name, meta_dict[video_name], face_dict[video_name], com_list, False, plot_c, False)
        else:
            anchor_group = None
            
        if anchor_group is None:
            anchor_dict[video_name] = []
        else:
            anchor_dict[video_name] = anchor_group
        if i % 50 == 0:
            pickle.dump(anchor_dict, open(anchor_dict_path, "wb" ))
    
    pickle.dump(anchor_dict, open(anchor_dict_path, "wb" ))
    print("Thread %d finished computing..." % (thread_id))
    out = open('../log/detect_anchor.txt', 'a')
    out.write('Thread ' + str(thread_id) + ' finished computing...\n')
    out.close()

def detect_anchor_parallel(video_list_path, anchor_dict_path=None, plot_c=False, nthread=16, use_process=True):
    ## log
    out = open('../log/detect_anchor.txt', 'w')
    out.write('init\n')
    out.close()
    
    if not video_list_path is None:
        video_list = open(video_list_path).read().split('\n')
    
        dict_file = Path(anchor_dict_path)
        if dict_file.is_file():
            anchor_dict = pickle.load(open(anchor_dict_path, "rb" ))
            video_list = [video for video in video_list if video not in anchor_dict]
        else:
            anchor_dict = {}

        num_video = len(video_list)
        print(num_video)
        if num_video == 0:
            return 
        if num_video <= nthread:
            nthread = num_video
            num_video_t = 1
        else:
            num_video_t = math.ceil(1. * num_video / nthread)
        print(num_video_t)
    else:
        dict_file = Path(anchor_dict_path)
        if dict_file.is_file():
            anchor_dict = pickle.load(open(anchor_dict_path, "rb" ))
        else:
            anchor_dict = {}
        
    anchor_dict_list = []
    for i in range(nthread):
        anchor_dict_list.append('../tmp/anchor_dict_' + str(i+32) + '.pkl')

    if use_process:
        ctx = mp.get_context('spawn')
    thread_list = []
    for i in range(nthread):
        if not video_list_path is None:
            if i != nthread - 1:
                video_list_t = video_list[i*num_video_t : (i+1)*num_video_t]
            else:
                video_list_t = video_list[i*num_video_t : ]
        else:
            video_list_t = None
        if use_process:
            t = ctx.Process(target=detect_anchor_t, args=(video_list_t, anchor_dict_list[i], plot_c, i,))
        else:
            t = threading.Thread(target=detect_anchor_t, args=(video_list_t, anchor_dict_list[i], plot_c, i,))
            t.setDaemon(True)
        thread_list.append(t)
    
    for t in thread_list:
        t.start()
    for t in thread_list:
        t.join()
    
    for path in anchor_dict_list:
        dict_file = Path(path)
        if not dict_file.is_file():
            continue
        anchor_dict_tmp = pickle.load(open(path, "rb" ))
#         anchor_dict = {**anchor_dict, **anchor_dict_tmp}
        for key, value in anchor_dict_tmp.items():
            anchor_dict[key] = value
    
    pickle.dump(anchor_dict, open(anchor_dict_path, "wb" ), protocol=2)  
    
#     # post process
#     people_dict = build_people_dict(anchor_dict)
#     anchor_dict = clean_anchor_dict(anchor_dict, people_dict)

#     pickle.dump(anchor_dict, open(anchor_dict_path, "wb" ))  